from pathlib import Path
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def generate_pdf_report(
    report: dict,
    output_path: str | Path,
    map_output_path: str | Path | None = None,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title=f"HazardMind AI Report - {report['event_id']}",
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    heading_style = styles["Heading2"]
    normal_style = styles["BodyText"]

    elements = [
        Paragraph("HazardMind AI Executive Disaster Report", title_style),
        Spacer(1, 0.15 * inch),
        _section_table(
            [
                ("Event ID", report["event_id"]),
                ("Location", report["location"]),
                ("Hazard Type", report["hazard_type"]),
                ("Overall Severity", report["overall_severity"]),
                ("Satellite Type", report["satellite"]["type"]),
                ("Satellite Reason", report["satellite"]["reason"]),
            ]
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Operational Statistics", heading_style),
        _section_table(
            [
                ("Affected Area", f"{report['analysis']['affected_area_km2']} km2"),
                ("Damage Percent", f"{report['analysis']['damage_percent']}%"),
                ("Total Zones", report["analysis"]["total_zones"]),
                ("Population Affected", f"{report['impact']['population_affected']:,}"),
                ("Hospitals at Risk", report["impact"]["hospitals_at_risk"]),
                ("Roads Blocked", f"{report['impact']['roads_blocked_km']} km"),
                ("Schools Affected", report["impact"]["schools_affected"]),
                ("Vulnerability Score", report["impact"]["vulnerability_score"]),
            ]
        ),
        Spacer(1, 0.18 * inch),
    ]

    map_path = Path(map_output_path) if map_output_path else None
    if map_path and map_path.exists():
        elements.extend(
            [
                Paragraph("Generated Risk Map", heading_style),
                Image(str(map_path), width=6.5 * inch, height=4.06 * inch),
                Spacer(1, 0.18 * inch),
            ]
        )

    elements.extend(
        [
            Paragraph("Executive Summary", heading_style),
            _paragraph(report["report"]["summary"], normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Detailed Incident Analysis", heading_style),
            _paragraph(report["report"].get("detailed_body", ""), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Technical Analysis", heading_style),
            _paragraph(report["report"].get("technical_analysis", ""), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Recommendations", heading_style),
            _bullet_list(report["report"].get("recommendations", []), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Response Priorities", heading_style),
            _bullet_list(report["report"].get("response_priorities", []), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Assumptions", heading_style),
            _bullet_list(report["report"].get("assumptions", []), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Limitations", heading_style),
            _bullet_list(report["report"].get("limitations", []), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Model Source Note", heading_style),
            _paragraph(model_source_note(report), normal_style),
            Spacer(1, 0.08 * inch),
            _section_table(
                [
                    ("Detailed Report", report.get("model_sources", {}).get("detailed_report", "unknown")),
                    ("Executive Summary", report.get("model_sources", {}).get("executive_summary", "unknown")),
                    ("Fallback Used", report.get("model_sources", {}).get("fallback_used", "unknown")),
                    ("Featherless Model", report.get("model_sources", {}).get("featherless_model", "unknown")),
                ]
            ),
            Spacer(1, 0.18 * inch),
            Paragraph("Agent Log / Trace", heading_style),
            _agent_log_table(report["agent_log"]),
        ]
    )

    doc.build(elements)
    return path


def _section_table(rows: list[tuple[str, object]]) -> Table:
    table = Table(
        [[label, str(value)] for label, value in rows],
        colWidths=[1.8 * inch, 4.7 * inch],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF2F8")),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#17202A")),
                ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1B2631")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AEB6BF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def model_source_note(report: dict) -> str:
    sources = report.get("model_sources", {})
    if sources.get("fallback_used"):
        return "Featherless detailed generation failed; AI/ML fallback was used. Executive summary generated using AI/ML API."
    return "Detailed report generated using Featherless Kimi K2.6. Executive summary generated using AI/ML API."


def _bullet_list(items: list[str], style) -> ListFlowable:
    return ListFlowable(
        [ListItem(_paragraph(item, style)) for item in items],
        bulletType="bullet",
        start="circle",
        leftIndent=18,
    )


def _paragraph(text: object, style) -> Paragraph:
    return Paragraph(escape(str(text or "")), style)


def _agent_log_table(agent_log: list[dict]) -> Table:
    rows = [["Agent", "Status", "Timestamp", "Message"]]
    rows.extend(
        [
            [
                entry["agent"],
                entry["status"],
                entry["timestamp"],
                entry["message"],
            ]
            for entry in agent_log
        ]
    )

    table = Table(
        rows,
        colWidths=[1.35 * inch, 0.75 * inch, 1.35 * inch, 3.05 * inch],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#07111F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F8FBFD")),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#1B2631")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AEB6BF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table
