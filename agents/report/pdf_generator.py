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
            Paragraph("Intelligence Assessment", heading_style),
            _intelligence_summary_table(report),
            Spacer(1, 0.14 * inch),
            Paragraph("Map Narrative", heading_style),
            _paragraph(report.get("intelligence", {}).get("map_narrative", {}).get("map_narrative", ""), normal_style),
            Spacer(1, 0.08 * inch),
            _bullet_list(
                report.get("intelligence", {}).get("map_narrative", {}).get("key_spatial_findings", []),
                normal_style,
            ),
            Spacer(1, 0.18 * inch),
            Paragraph("Priority Timeline", heading_style),
            _priority_timeline_table(report),
            Spacer(1, 0.18 * inch),
            Paragraph("Anomalies and Warnings", heading_style),
            _bullet_list(_anomaly_warning_items(report), normal_style),
            Spacer(1, 0.18 * inch),
            Paragraph("Quality Check", heading_style),
            _quality_check_table(report),
            Spacer(1, 0.18 * inch),
            Paragraph("Band-Ready Final Message", heading_style),
            _paragraph(report.get("intelligence", {}).get("band_ready_message", {}).get("message", ""), normal_style),
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
                    ("Criticality", report.get("model_sources", {}).get("intelligence", {}).get("criticality", "unknown")),
                    ("Map Narrative", report.get("model_sources", {}).get("intelligence", {}).get("map_narrative", "unknown")),
                    (
                        "Priority Timeline",
                        report.get("model_sources", {}).get("intelligence", {}).get(
                            "priority_recommendations", "unknown"
                        ),
                    ),
                    ("Quality Check", report.get("model_sources", {}).get("intelligence", {}).get("quality_check", "unknown")),
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
    intelligence_sources = sources.get("intelligence", {})
    intelligence_note = ", ".join(
        f"{label}: {source}" for label, source in intelligence_sources.items() if source
    )
    if sources.get("fallback_used"):
        return (
            "Featherless detailed generation failed; AI/ML fallback was used. "
            f"Executive and intelligence outputs were generated with safe fallbacks where needed. {intelligence_note}"
        )
    return (
        "Detailed report generated using Featherless Kimi K2.6. Executive summary generated using AI/ML API. "
        f"Intelligence sources: {intelligence_note}"
    )


def _bullet_list(items: list[str], style) -> ListFlowable:
    if not items:
        items = ["None reported."]
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


def _intelligence_summary_table(report: dict) -> Table:
    intelligence = report.get("intelligence", {})
    criticality = intelligence.get("criticality", {})
    decision = intelligence.get("decision_brief", {})
    return _section_table(
        [
            ("Criticality", criticality.get("criticality", "unknown")),
            ("Overall Confidence", f"{round(float(criticality.get('overall_confidence', 0)) * 100)}%"),
            ("Escalation Required", criticality.get("escalation_required", "unknown")),
            ("Rationale", criticality.get("rationale", "")),
            ("Key Decisions", "; ".join(decision.get("key_decisions_required", []))),
            ("Human Review Required", decision.get("human_review_required", "unknown")),
        ]
    )


def _priority_timeline_table(report: dict) -> Table:
    timeline = report.get("intelligence", {}).get("priority_timeline", {})
    return _section_table(
        [
            ("Next 6 Hours", "; ".join(timeline.get("next_6_hours", []))),
            ("Next 24 Hours", "; ".join(timeline.get("next_24_hours", []))),
            ("Next 72 Hours", "; ".join(timeline.get("next_72_hours", []))),
            ("Resources", "; ".join(timeline.get("resource_priorities", []))),
            ("Coordination", "; ".join(timeline.get("coordination_priorities", []))),
        ]
    )


def _quality_check_table(report: dict) -> Table:
    quality = report.get("intelligence", {}).get("quality_check", {})
    checks = quality.get("checks", {})
    rows = [("Status", quality.get("status", "unknown"))]
    rows.extend((label.replace("_", " ").title(), value) for label, value in checks.items())
    rows.append(("Warnings", "; ".join(quality.get("warnings", []))))
    rows.append(("Blocking Issues", "; ".join(quality.get("blocking_issues", []))))
    return _section_table(rows)


def _anomaly_warning_items(report: dict) -> list[str]:
    anomalies = report.get("intelligence", {}).get("anomalies", {})
    items = [
        f"{item.get('severity', 'low').upper()}: {item.get('description', '')} "
        f"Handling: {item.get('recommended_handling', '')}"
        for item in anomalies.get("anomalies", [])
    ]
    items.extend(report.get("intelligence", {}).get("quality_check", {}).get("warnings", []))
    return items or ["No blocking anomalies detected."]
