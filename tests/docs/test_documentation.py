"""CI tests that fail the build when implementation drifts from documented values."""
import asyncio
import re
from pathlib import Path

from ac_infinity_mcp.analytics import STAGE_TARGETS
from ac_infinity_mcp.server import mcp_server

REPO_ROOT = Path(__file__).parents[2]
GUIDE = (REPO_ROOT / "docs/GUIDE.md").read_text()
README = (REPO_ROOT / "README.md").read_text()
API_MD = (REPO_ROOT / "docs/API.md").read_text()


def _registered_tool_names() -> list[str]:
    return [t.name for t in asyncio.run(mcp_server.list_tools())]


def test_guide_tool_names() -> None:
    """Every tool name in the GUIDE.md appendix table is a registered tool."""
    appendix_section = re.search(
        r"## 12\. Appendix.*?(\|.*?)(?=\n##|\Z)", GUIDE, re.DOTALL
    )
    assert appendix_section, "Appendix section not found in GUIDE.md"
    doc_names = set(re.findall(r"\|\s*`(\w+)`", appendix_section.group(1)))
    registered = set(_registered_tool_names())
    assert doc_names == registered, (
        f"GUIDE.md appendix tools differ from registered tools.\n"
        f"  In docs only: {doc_names - registered}\n"
        f"  In code only: {registered - doc_names}"
    )


def test_guide_tool_count() -> None:
    """Tool count in GUIDE.md appendix title matches actual registered tool count."""
    match = re.search(r"All (\d+) Tools", GUIDE)
    assert match, "Could not find 'All N Tools' in GUIDE.md"
    doc_count = int(match.group(1))
    actual_count = len(_registered_tool_names())
    assert doc_count == actual_count, (
        f"GUIDE.md says 'All {doc_count} Tools' but {actual_count} tools are registered"
    )


def test_guide_stage_vpd_targets() -> None:
    """Every 'VPD target: X.X kPa' in GUIDE.md matches a known STAGE_TARGETS midpoint."""
    known_midpoints = {
        stage: (v["vpd"][0] + v["vpd"][1]) / 2
        for stage, v in STAGE_TARGETS.items()
    }
    doc_values = [float(v) for v in re.findall(r"VPD target:\s*([\d.]+)\s*kPa", GUIDE)]
    assert doc_values, "No 'VPD target: X.X kPa' entries found in GUIDE.md"
    for value in doc_values:
        matches = [
            stage
            for stage, mid in known_midpoints.items()
            if abs(value - mid) <= 0.05
        ]
        assert matches, (
            f"VPD target {value} kPa in GUIDE.md does not match any STAGE_TARGETS midpoint "
            f"(known midpoints: {known_midpoints})"
        )


def test_guide_no_dry_run_term() -> None:
    """The string 'dry_run' does not appear anywhere in GUIDE.md."""
    assert "dry_run" not in GUIDE, (
        "GUIDE.md contains the internal term 'dry_run' — use 'preview' or natural language instead"
    )


def test_guide_plug_status_value() -> None:
    """GUIDE.md uses the canonical plug-status string 'not powered'."""
    assert '"not powered"' in GUIDE or "'not powered'" in GUIDE, (
        "GUIDE.md does not contain the canonical plug-status value 'not powered'"
    )


def test_readme_tool_count() -> None:
    """Tool rows in README.md tool table match the actual registered tool count."""
    tools_section = re.search(r"## Tools\n(.*?)(?=\n## )", README, re.DOTALL)
    assert tools_section, "Could not find '## Tools' section in README.md"
    tool_rows = [
        line
        for line in tools_section.group(1).splitlines()
        if re.match(r"\|.*`\w+`", line)
    ]
    actual_count = len(_registered_tool_names())
    assert len(tool_rows) == actual_count, (
        f"README.md tool table has {len(tool_rows)} rows but {actual_count} tools are registered"
    )


def test_api_md_quirk_count() -> None:
    """Header 'All N Known API Quirks' in API.md matches the count of '### Quirk N' entries."""
    header_match = re.search(r"All (\d+) Known API Quirks", API_MD)
    assert header_match, "Could not find 'All N Known API Quirks' header in API.md"
    header_count = int(header_match.group(1))
    entry_count = len(re.findall(r"^### Quirk \d+", API_MD, re.MULTILINE))
    assert header_count == entry_count, (
        f"API.md header says {header_count} quirks but found {entry_count} '### Quirk N' entries"
    )


def test_api_md_quirk2_documents_25_char_limit() -> None:
    """Quirk 2 must keep documenting the 25-character password limit (#262/#263).

    The auth-failure message surfaced to growers cites this limit; this pins the
    documented value to the Quirk 2 section so the two cannot silently drift apart.
    """
    section = re.search(
        r"^### Quirk 2 .*?(?=^### Quirk 3)", API_MD, re.MULTILINE | re.DOTALL
    )
    assert section, "Could not isolate the Quirk 2 section in API.md"
    assert "25 character" in section.group(0), (
        "Quirk 2 section no longer documents the 25-character password limit"
    )
