"""Parity tests between Pydantic wire schemas, WS dispatcher, and TypeScript."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from quartz.web.schemas import CLIENT_FRAME_TYPES, ClientFrame, parse_client_frame

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
WS_ROUTE = ROOT / "src" / "quartz" / "web" / "routes" / "ws.py"
PROTOCOL_TS = ROOT / "webapp" / "src" / "protocol.ts"
GENERATED_TS = ROOT / "webapp" / "src" / "protocol.generated.ts"


def _schema_frame_types() -> set[str]:
    # Extract Literal["..."] type tags from the ClientFrame union members.
    union_type = get_args(ClientFrame)[0]
    tags: set[str] = set()
    for model in get_args(union_type):
        literal_type = model.model_fields["type"].annotation
        tags.update(get_args(literal_type))
    return tags


def test_client_frame_types_match_ws_dispatch_and_typescript() -> None:
    schema_types = _schema_frame_types()
    assert schema_types == CLIENT_FRAME_TYPES

    ws_source = WS_ROUTE.read_text(encoding="utf-8")
    protocol_ts = PROTOCOL_TS.read_text(encoding="utf-8")
    generated_ts = GENERATED_TS.read_text(encoding="utf-8")

    # Every frame type in schemas.py must be handled in ws.py and present in both TS files.
    for frame_type in sorted(schema_types):
        assert f'type: "{frame_type}"' in protocol_ts, f"{frame_type} missing from protocol.ts"
        assert f'type: "{frame_type}";' in generated_ts, (
            f"{frame_type} missing from protocol.generated.ts"
        )
        # ws.py dispatches via isinstance(frame, <ModelName>)
        assert frame_type in ws_source or any(
            m.__name__ in ws_source
            for m in get_args(get_args(ClientFrame)[0])
            if frame_type in get_args(m.model_fields["type"].annotation)
        ), f"{frame_type} not handled in routes/ws.py"

    # Every client->server type in protocol.ts must also be in schemas.py.
    client_block = protocol_ts.split("/* ------------------------------------------------------- client -> server */")[1]
    client_block = client_block.split("/* ------------------------------------------------------- server -> client */")[0]
    ts_types = set(re.findall(r'type:\s*"([a-z0-9_]+)"', client_block))
    assert ts_types == schema_types


def test_generated_typescript_has_no_drift() -> None:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import export_web_schema

    expected = export_web_schema.render_typescript()
    actual = GENERATED_TS.read_text(encoding="utf-8")
    assert actual == expected


def test_parse_client_frame_validates_and_rejects_bad_frames() -> None:
    hello = parse_client_frame({"type": "hello", "clientVersion": 1, "systemPromptAppend": 123})
    assert hello.type == "hello"
    assert hello.system_prompt_append is None

    with pytest.raises(ValidationError):
        parse_client_frame({"type": "set_sandbox", "enabled": "no"})

    with pytest.raises(ValidationError):
        parse_client_frame({"type": "nonsense"})
