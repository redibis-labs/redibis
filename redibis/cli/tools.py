from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolDescriptor:
    name:         str
    description:  str
    inputs:       dict[str, str]
    returns:      str
    idempotent:   bool
    expensive:    bool
    category:     str

    def to_anthropic_tool(self) -> dict:
        return {
            "name":        self.name,
            "description": self.description,
            "input_schema": {
                "type":       "object",
                "properties": {
                    k: {"type": "string", "description": v}
                    for k, v in self.inputs.items()
                },
                "required":   list(self.inputs.keys()),
            },
        }


TOOL_CATALOG: list[ToolDescriptor] = []


def _emit(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif isinstance(obj, dict) and "items" in obj:
        items = obj.get("items") or []
        for p in items:
            print(f"  {p.get('prop_id','?'):24s}  {p.get('kind',''):8s}  "
                  f"{p.get('column',''):20s}  {p.get('label',''):30s}  {p.get('status','')}")
    else:
        print(json.dumps(obj, indent=2, default=str))


def cmd_approved(args, store, backend) -> int:
    from redibis.services.session_service import (
        build_approved_partials, merge_approved, load_session_from_dir,
        pii_row_to_fragment, quality_row_to_fragment,
    )
    session = load_session_from_dir(args.session)
    a = session.approved
    action = args.approved_action

    if action == "list":
        _emit(a.to_dict(), args.json)
    elif action == "show":
        p = a.get(args.prop)
        if p is None:
            print(f"No approved property {args.prop!r}", file=__import__("sys").stderr)
            return 1
        _emit(p.to_dict(), args.json)
    elif action == "add-pii":
        with open(args.from_json, encoding="utf-8") as f:
            row = json.load(f)
        session.approve_property(
            kind="pii", column=args.column or row.get("column"),
            payload=pii_row_to_fragment(row), source="manual",
            label=row.get("entity_type", ""))
        session.persist_to_disk()
        print("Added PII property for", args.column or row.get("column"))
    elif action == "add-quality":
        with open(args.from_json, encoding="utf-8") as f:
            row = json.load(f)
        session.approve_property(
            kind="quality", column=args.column or "__table__",
            payload=quality_row_to_fragment(row), source="manual",
            label=row.get("expectation_type") or row.get("rule", ""))
        session.persist_to_disk()
        print("Added quality property")
    elif action == "remove":
        if not a.remove(args.prop):
            print(f"No approved property {args.prop!r}", file=__import__("sys").stderr)
            return 1
        session.persist_to_disk()
        print("Removed", args.prop)
    elif action == "preview":
        _emit(build_approved_partials(session), True)
    elif action == "merge":
        res = merge_approved(session, store, validate=not args.no_validate)
        session.persist_to_disk()
        _emit(res, args.json)
    elif action == "clear":
        a.clear(only_merged=getattr(args, "only_merged", False))
        session.persist_to_disk()
        print("Cleared approved basket")
    else:
        return 1
    return 0


def cmd_session(args) -> int:
    from redibis.services.session import loader as load_module
    from redibis.services.session.sources import list_sources

    action = args.session_action
    if action == "load":
        try:
            result = load_module.load_session(args.run_id, args.source)
        except FileNotFoundError as exc:
            print(str(exc), file=__import__("sys").stderr)
            return 1
        except (KeyError, ValueError) as exc:
            print(str(exc), file=__import__("sys").stderr)
            return 1
        _emit(result, args.json)
        return 0
    if action == "sources":
        rows = [{"name": s.name, "label": s.label, "root": str(s.root())} for s in list_sources()]
        if args.json:
            _emit(rows, True)
        else:
            for row in rows:
                print(f"  {row['name']:8s}  {row['label']:20s}  {row['root']}")
        return 0
    if action == "list":
        try:
            sessions = load_module.list_available(args.source)
        except KeyError as exc:
            print(str(exc), file=__import__("sys").stderr)
            return 1
        if args.json:
            _emit({"source": args.source, "sessions": sessions}, True)
        else:
            for s in sessions:
                print(f"  {s.get('session_id','?'):36s}  {s.get('table_name',''):24s}  "
                      f"{s.get('status',''):16s}  {s.get('created_at','')[:10]}")
        return 0
    return 1
