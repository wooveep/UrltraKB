"""Small, explicit renderer adaptations. Never change the user's original Markdown."""

import re
import xml.etree.ElementTree as ET

SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


def adapt_diagram(svg, dark):
    root = ET.fromstring(svg)
    changes = []
    for marker in root.iter(f"{{{SVG}}}marker"):
        classes = marker.get("class", "").split()
        if "extension" in classes or "aggregation" in classes:
            for shape in marker:
                # Inheritance/aggregation must remain hollow. The original CSS inherited
                # transparent!important from marker; resvg does not preserve that here.
                shape.set(
                    "style",
                    "fill:none;stroke:" + ("#cccccc" if dark else "#333333") + ";stroke-width:1",
                )
            changes.append("explicit hollow UML marker")
    if root.get("aria-roledescription") == "c4":
        for node in root.iter():
            if node.tag == f"{{{SVG}}}text":
                node.set("fill", "#e2e8f0" if dark else "#0f172a")
            if node.tag == f"{{{SVG}}}tspan":
                node.attrib.pop("alignment-baseline", None)
            if "font-family" in node.attrib:
                node.set("font-family", "Noto Sans CJK SC")
            if "style" in node.attrib:
                node.set(
                    "style",
                    re.sub(r"font-family:[^;]+", "font-family:Noto Sans CJK SC", node.get("style")),
                )
        changes.append("C4 bundled font and inherited dominant baseline")
        # Merman 0.7.0 passes the edge midpoint as a left edge to its C4 text writer,
        # which adds half the label width again. Place labels over the existing edge
        # geometry; do not alter node positions, endpoints, directions or source text.
        for group in root.findall(f"{{{SVG}}}g"):
            midpoint = None
            original_mid_x = 0
            row = 0
            for node in list(group):
                if node.tag == f"{{{SVG}}}line" and node.get("marker-end"):
                    midpoint = (
                        (float(node.get("x1")) + float(node.get("x2"))) / 2,
                        (float(node.get("y1")) + float(node.get("y2"))) / 2,
                    )
                    row = 0
                    original_mid_x = midpoint[0]
                elif node.tag == f"{{{SVG}}}path" and node.get("marker-end"):
                    numbers = re.findall(
                        r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", node.get("d", "")
                    )
                    if len(numbers) != 6 or not re.fullmatch(
                        r"M[^A-Za-z]*Q[^A-Za-z]*", node.get("d", "")
                    ):
                        raise ValueError(
                            "C4 relation geometry outside the verified renderer geometry"
                        )
                    x1, y1, cx, cy, x2, y2 = map(float, numbers)
                    midpoint = ((x1 + 2 * cx + x2) / 4, (y1 + 2 * cy + y2) / 4)
                    original_mid_x = (x1 + x2) / 2
                    row = 0
                elif node.tag == f"{{{SVG}}}text" and midpoint:
                    label_width = max(0, 2 * (float(node.get("x")) - original_mid_x))
                    node.set("x", str(midpoint[0]))
                    node.set("y", str(midpoint[1] - 28 + 17 * row))
                    node.set("fill", "#e2e8f0" if dark else "#334155")
                    background = ET.Element(
                        f"{{{SVG}}}rect",
                        {
                            "x": str(midpoint[0] - label_width / 2 - 3),
                            "y": str(float(node.get("y")) - 9),
                            "width": str(label_width + 6),
                            "height": "18",
                            "style": "fill:" + ("#0f172a" if dark else "#ffffff") + ";stroke:none",
                        },
                    )
                    group.insert(list(group).index(node), background)
                    row += 1
                if midpoint and node.tag in (f"{{{SVG}}}line", f"{{{SVG}}}path"):
                    node.set("stroke", "#94a3b8" if dark else "#475569")
        changes.append("C4 relationship labels centered above existing straight/quadratic edges")
    return ET.tostring(root, encoding="unicode"), sorted(set(changes))
