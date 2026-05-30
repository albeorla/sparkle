"""Sparkle CLI — a thin formatter over :mod:`sparkle.ops`.

Every command parses its arguments with argparse, makes exactly one call into
``ops.py`` (the single stdlib-pure contract every front-end shares), and prints
the result. The graph mutation/query logic, the debate invariants, the referee,
and the dedup gate all live in ``ops.py`` behind a single ``ValueError``
boundary — this file never touches the store directly. That is what keeps the
CLI, the MCP server, and any future harness interchangeable.

Error contract (unchanged): a user-facing ``ValueError`` from ``ops.py`` is
printed to stderr and the process exits 2; an argparse ``SystemExit`` carries
its own code.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sys

from . import ops
from .templates import BRANCH_TEMPLATES

VALID_NODE_TYPES = ops.VALID_NODE_TYPES
VALID_NODE_STATUSES = ops.VALID_NODE_STATUSES
VALID_EDGE_RELATIONS = ops.VALID_EDGE_RELATIONS


def confidence_arg(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: '{raw}'")
    if not (0.0 <= value <= 1.0):
        raise argparse.ArgumentTypeError(f"confidence must be between 0.0 and 1.0, got {value}")
    return value


DEFAULT_STORE = Path(".sparkle/graph.json")

# The autonomous run shells out to two vendor CLIs (verified invocations:
# `claude -p ...` and `codex exec ...`). Both must be installed and logged in
# on the user's machine; we check PATH presence before starting a run so the
# failure is an actionable install/login hint rather than a deep subprocess
# crash. These are the bare binary names, not pip packages.
RUN_REQUIRED_CLIS = ("claude", "codex")

TYPE_SYMBOLS = {
    "claim": "C",
    "evidence": "+",
    "question": "?",
    "objection": "-",
    "inference": "~",
    "decision": "!",
    "synthesis": ">",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sparkle claim-graph MVP")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="Path to graph store JSON file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the local graph store")
    subparsers.add_parser("home", help="Show a dashboard for the current graph")
    list_nodes = subparsers.add_parser("list-nodes", help="List nodes in the graph")
    list_nodes.add_argument("--type", dest="filter_type", choices=VALID_NODE_TYPES)
    list_nodes.add_argument("--status", choices=VALID_NODE_STATUSES)
    list_nodes.add_argument("--tag")
    list_nodes.add_argument("--query")
    list_nodes.add_argument("--limit", type=int)
    list_nodes.add_argument(
        "--ids-only",
        action="store_true",
        help="Print only short handles, one per line, for fast scanning before linking",
    )
    subparsers.add_parser("list-edges", help="List edges in the graph")
    subparsers.add_parser("bootstrap", help="Seed the store from the original concept conversation")
    subparsers.add_parser("list-templates", help="List structured branch templates")
    subparsers.add_parser("relations", help="Show each edge relation with a direction gloss")

    add_node = subparsers.add_parser("add-node", help="Add a typed node")
    add_node.add_argument("--type", required=True, dest="node_type", choices=VALID_NODE_TYPES)
    add_node.add_argument("--title", required=True)
    add_node.add_argument("--content", required=True)
    add_node.add_argument("--citations", nargs="*", default=[])
    add_node.add_argument("--author", default="local")
    add_node.add_argument("--confidence", type=confidence_arg, default=0.5)
    add_node.add_argument("--status", default="active", choices=VALID_NODE_STATUSES)
    add_node.add_argument("--tags", nargs="*", default=[])
    add_node.add_argument(
        "--link-to",
        dest="link_to",
        help="Existing node id-or-prefix to link the new node to (requires --relation)",
    )
    add_node.add_argument(
        "--relation",
        choices=VALID_EDGE_RELATIONS,
        help="Relation for the fused link (requires --link-to); new node is the source",
    )
    add_node.add_argument(
        "--as",
        dest="alias",
        help="Register an alias name for the new node (stored in .sparkle/aliases.json)",
    )

    add_edge = subparsers.add_parser("add-edge", help="Link two nodes")
    add_edge.add_argument("--from", required=True, dest="from_id")
    add_edge.add_argument("--to", required=True, dest="to_id")
    add_edge.add_argument("--relation", required=True, choices=VALID_EDGE_RELATIONS)
    add_edge.add_argument("--note", default="")

    add_branch = subparsers.add_parser("add-branch", help="Create a structured inquiry branch from an existing node")
    add_branch.add_argument("--from", required=True, dest="from_id")
    add_branch.add_argument("--template", required=True)
    add_branch.add_argument("--title", required=True)
    add_branch.add_argument("--content")
    add_branch.add_argument("--citations", nargs="*", default=[])
    add_branch.add_argument("--author", default="local")
    add_branch.add_argument("--confidence", type=confidence_arg, default=0.5)
    add_branch.add_argument("--tags", nargs="*", default=[])

    revise = subparsers.add_parser(
        "revise", help="Supersede a node with a corrected version (never mutates)"
    )
    revise.add_argument("ref")
    revise.add_argument("--title")
    revise.add_argument("--content")
    revise.add_argument("--confidence", type=confidence_arg)
    revise.add_argument("--status", choices=VALID_NODE_STATUSES)
    revise.add_argument("--tags", nargs="*")
    rehome = revise.add_mutually_exclusive_group()
    rehome.add_argument(
        "--rehome-edges",
        dest="rehome_edges",
        action="store_true",
        default=True,
        help="Re-point the old node's inbound edges at the new version (default)",
    )
    rehome.add_argument(
        "--no-rehome-edges",
        dest="rehome_edges",
        action="store_false",
        help="Leave the old node's inbound edges in place on the superseded version",
    )

    import_cmd = subparsers.add_parser(
        "import", help="Build a graph fragment from a JSON document (file or '-')"
    )
    import_cmd.add_argument(
        "source",
        help="Path to a JSON document, or '-' to read JSON from stdin",
    )

    mcp_cmd = subparsers.add_parser(
        "mcp", help="Run the MCP server (requires the 'sparkle[mcp]' extra)"
    )
    mcp_cmd.set_defaults(_mcp=True)

    run_cmd = subparsers.add_parser(
        "run",
        help="Run the autonomous adversarial loop (requires the 'claude' and 'codex' CLIs on PATH)",
    )
    run_cmd.add_argument("seed", help="the seed question or claim to debate")
    run_cmd.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="override max critique/gather rounds",
    )
    run_cmd.add_argument("--run-id", dest="run_id", default=None)
    run_cmd.add_argument(
        "--max-moves",
        type=int,
        default=None,
        help="hard iteration cap (runaway backstop)",
    )
    run_cmd.set_defaults(_run=True)

    show = subparsers.add_parser("show", help="Show a node with inbound and outbound edges")
    show.add_argument("node_id")

    tree = subparsers.add_parser("tree", help="Render a local ASCII tree for a node")
    tree.add_argument("node_id")

    why = subparsers.add_parser("why", help="Render inbound provenance for a node")
    why.add_argument("node_id")

    lineage = subparsers.add_parser("lineage", help="Trace inbound lineage for a node")
    lineage.add_argument("node_id")

    export = subparsers.add_parser("export", help="Export a subgraph to markdown")
    export.add_argument("--root", required=True, dest="root_id")
    export.add_argument("--output", type=Path)

    return parser


# ---------------------------------------------------------------------------
# Presentation helpers — formatting of the dicts ops.py returns
# ---------------------------------------------------------------------------


def print_node_summary(node: dict) -> None:
    c = node.get("confidence")
    conf = f"{c:.2f}" if c is not None else "n/a"
    print(
        f"{node['handle']}  {node['node_type']:<10}  {node['status']:<16}  "
        f"{conf}  {node['title']}"
    )


def format_related_node(node: dict) -> str:
    symbol = TYPE_SYMBOLS.get(node["node_type"], "*")
    return f"{symbol} {node['node_type']:<10} {node['handle']}  {node['title']}"


def print_relation_groups(title: str, items: list[dict]) -> None:
    if not items:
        return
    print(title)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["relation"]].append(item)

    for relation in sorted(grouped):
        print(f"  {relation}")
        for item in grouped[relation]:
            print(f"    {format_related_node(item)}")


def print_kv_counts(title: str, counts: dict[str, int]) -> None:
    if not counts:
        return
    print(title)
    for key in sorted(counts):
        print(f"  {key:<18} {counts[key]}")


def print_link(link: dict) -> None:
    """Spoken-word echo so a user catches a backwards edge at a glance."""
    print(
        f'Linked: {link["from_type"]} "{link["from_title"]}" '
        f'--{link["relation"]}--> {link["to_type"]} "{link["to_title"]}"'
    )


def _read_import_document(source: str) -> dict:
    if source == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read import source {source!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in import source: {exc}") from exc


# ---------------------------------------------------------------------------
# Per-command handlers (each: one ops call + printing)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    prog = parser.prog

    # The MCP subcommand is a lazy seam: import the optional server only when
    # asked, so every other command works without the 'sparkle[mcp]' extra.
    if getattr(args, "_mcp", False):
        try:
            from .mcp_server import run_server
        except ImportError:
            print("pip install 'sparkle[mcp]'", file=sys.stderr)
            return 2
        run_server(args.store)
        return 0

    # The autonomous-run subcommand is a lazy seam exactly like the MCP one:
    # importing the harness is deferred until asked, so every other command
    # works without ever touching the thinker layer. The harness and thinker
    # modules are now pure stdlib (the run shells out to the `claude` and
    # `codex` CLIs via subprocess — no SDK, no pip extra), so the import always
    # succeeds. The real failure is a missing CLI at run time: if either binary
    # is absent from PATH the run cannot talk to a model, so we check first and
    # raise an actionable install/login hint through the same ValueError ->
    # stderr + exit-2 contract every other command uses.
    if getattr(args, "_run", False):
        from .graph import GraphStore

        try:
            missing = [name for name in RUN_REQUIRED_CLIS if shutil.which(name) is None]
            if missing:
                raise ValueError(
                    f"required CLI(s) not found on PATH: {', '.join(missing)}. "
                    "Install and log in to the claude and codex CLIs "
                    "(the run uses `claude -p` on your Claude Max login and "
                    "`codex exec` on your Codex/ChatGPT login), then re-run."
                )

            from .harness import run_cli_loop

            result = run_cli_loop(
                store=GraphStore(args.store),
                seed=args.seed,
                run_id=args.run_id,
                rounds=args.rounds,
                max_moves=args.max_moves,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        print(f"Run: {result['run_id']}")
        if result.get("claim_id"):
            print(f"Handle: {result['claim_id'][:12]}")
        print(f"Status: {result.get('status', 'done')}")
        print(f"Moves: {len(result.get('moves', []))}")
        signal = result.get("final_signal")
        if signal is not None:
            print(f"Final signal: {signal}")
        return 0

    from .graph import GraphStore

    store = GraphStore(args.store)

    try:
        if args.command == "init":
            result = ops.init(store)
            print(f"Initialized graph store at {result['store']}")
            return 0

        if args.command == "bootstrap":
            result = ops.bootstrap(store)
            print("Seeded concept graph from original concept conversation")
            for label, node_id in result["ids"].items():
                print(f"{label}: {node_id}")
            return 0

        if args.command == "home":
            data = ops.home(store)
            print("SPARKLE HOME")
            print(f"Store: {data['store']}")
            print(f"Nodes: {data['node_count']}")
            print(f"Edges: {data['edge_count']}")
            print("")

            if data["empty"]:
                print("Graph is empty.")
                print("")
                print("Next:")
                print(f"  {prog} bootstrap")
                print(f'  {prog} add-node --type claim --title "..." --content "..."')
                return 0

            print_kv_counts("By type", data["type_counts"])
            print("")
            print_kv_counts("By status", data["status_counts"])
            print("")
            print("Recent")
            for node in data["recent"]:
                print(
                    f"  {node['handle']}  {node['node_type']:<10} "
                    f"{node['status']:<16} {node['title']}"
                )
            print("")
            print("Next")
            print(f"  {prog} list-nodes --status active")
            print(f"  {prog} tree <node_id_prefix>")
            print(f"  {prog} why <node_id_prefix>")
            print(
                f'  {prog} add-branch --from <node_id_prefix> '
                f'--template support --title "..."'
            )
            return 0

        if args.command == "list-templates":
            for template in ops.list_templates():
                print(
                    f"{template['name']:<12} {template['node_type']:<10} "
                    f"{template['relation']:<12} {template['description']}"
                )
            return 0

        if args.command == "relations":
            for item in ops.relations():
                print(f"{item['relation']:<14} from --{item['relation']}--> to")
                print(f"  {item['gloss']}")
            return 0

        if args.command == "add-node":
            # Resolve an alias name in --link-to to a concrete id so a user can
            # link to a node by the name they registered with --as. Only resolve
            # when --relation is also present; otherwise let ops.add_node raise
            # the paired-or-error message instead of a misleading prefix error.
            link_to = args.link_to
            if args.link_to is not None and args.relation is not None:
                link_to = ops.resolve_alias(store, args.link_to)
            result = ops.add_node(
                store,
                node_type=args.node_type,
                title=args.title,
                content=args.content,
                citations=args.citations,
                author=args.author,
                confidence=args.confidence,
                status=args.status,
                tags=args.tags,
                link_to=link_to,
                relation=args.relation,
            )
            print(f"Added {result['node_type']}: {result['title']}")
            print(f"Handle: {result['handle']}")
            if "link" in result:
                print_link(result["link"])
            if args.alias:
                ops.set_alias(store, args.alias, result["node_id"])
            return 0

        if args.command == "add-edge":
            link = ops.add_edge(
                store,
                from_ref=ops.resolve_alias(store, args.from_id),
                to_ref=ops.resolve_alias(store, args.to_id),
                relation=args.relation,
                note=args.note,
            )
            print_link(link)
            print(f"Handle: {link['handle']}")
            return 0

        if args.command == "add-branch":
            result = ops.add_branch(
                store,
                from_ref=ops.resolve_alias(store, args.from_id),
                template=args.template,
                title=args.title,
                content=args.content,
                citations=args.citations,
                author=args.author,
                confidence=args.confidence,
                tags=args.tags,
            )
            print(f"Added {result['node_type']}: {result['title']}")
            print(f"Handle: {result['handle']}")
            print_link(result["link"])
            return 0

        if args.command == "revise":
            result = ops.revise(
                store,
                ops.resolve_alias(store, args.ref),
                title=args.title,
                content=args.content,
                confidence=args.confidence,
                status=args.status,
                tags=args.tags,
                rehome_edges=args.rehome_edges,
            )
            print(f"Revised {result['old_id'][:12]} -> {result['handle']}")
            print(f"Handle: {result['handle']}")
            if args.rehome_edges:
                print(f"Re-homed {len(result['rehomed'])} inbound edge(s) onto the new version")
            else:
                print("Left inbound edges on the superseded version (use --rehome-edges to move them)")
            return 0

        if args.command == "import":
            document = _read_import_document(args.source)
            result = ops.import_graph(store, document)
            print(
                f"Imported {result['created_nodes']} node(s), "
                f"{result['created_edges']} edge(s)"
            )
            if result["nicknames"]:
                print("Nicknames")
                for nickname in sorted(result["nicknames"]):
                    print(f"  {nickname:<16} {result['nicknames'][nickname][:12]}")
            return 0

        if args.command == "list-nodes":
            nodes = ops.list_nodes(
                store,
                node_type=args.filter_type,
                status=args.status,
                tag=args.tag,
                query=args.query,
                limit=args.limit,
            )
            if not nodes:
                if not args.ids_only:
                    print("No nodes found")
                return 0
            for node in nodes:
                if args.ids_only:
                    print(node["handle"])
                else:
                    print_node_summary(node)
            return 0

        if args.command == "list-edges":
            edges = ops.list_edges(store)
            if not edges:
                print("No edges found")
                return 0
            for edge in edges:
                print(
                    f"{edge['handle']}  {edge['from_id'][:12]} -[{edge['relation']}]-> "
                    f"{edge['to_id'][:12]}"
                )
            return 0

        if args.command == "show":
            node = ops.get_node(store, ops.resolve_alias(store, args.node_id))
            c = node.get("confidence")
            conf = f"{c:.2f}" if c is not None else "n/a"
            print(f"{node['node_type'].upper()}  {node['status']}  {conf}")
            print(node["title"])
            print(f"ID: {node['node_id']}")
            if node["tags"]:
                print(f"Tags: {', '.join(node['tags'])}")
            if node["citations"]:
                print(f"Citations: {', '.join(node['citations'])}")
            print("")
            print(node["content"])
            print("")
            print_relation_groups("Incoming", node["inbound"])
            print_relation_groups("Outgoing", node["outbound"])
            return 0

        if args.command == "tree":
            from . import presentation

            print(presentation.render_tree(store, ops.resolve_alias(store, args.node_id)), end="")
            return 0

        if args.command == "why":
            from . import presentation

            print(presentation.render_why(store, ops.resolve_alias(store, args.node_id)), end="")
            return 0

        if args.command == "lineage":
            for ancestor in ops.lineage(store, ops.resolve_alias(store, args.node_id)):
                print_node_summary(ancestor)
            return 0

        if args.command == "export":
            rendered = ops.export(store, ops.resolve_alias(store, args.root_id))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"Exported markdown to {args.output}")
            else:
                print(rendered)
            return 0

    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
