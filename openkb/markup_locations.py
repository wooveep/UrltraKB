"""Validate native markup coordinates at the common evidence boundary."""


def validate_markup_location(location):
    kind = location["kind"]
    if kind == "html":
        if not isinstance(location.get("dom_path"), str) or not location["dom_path"].startswith(
            "/"
        ):
            raise ValueError("HTML evidence needs a DOM path")
        if location.get("selection") not in {"body", "document"}:
            raise ValueError("HTML evidence needs a body selection policy")
        if "dom_id" in location and not isinstance(location["dom_id"], str):
            raise ValueError("Invalid DOM anchor")
    elif set(location) & {"dom_path", "dom_id", "selection"}:
        raise ValueError("DOM positions require an HTML source")
    if kind == "xml":
        if not isinstance(location.get("element_path"), str) or not location[
            "element_path"
        ].startswith("/"):
            raise ValueError("XML evidence needs an element path")
        for key in ("namespaces", "attributes"):
            value = location.get(key)
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError("Invalid XML metadata")
    elif set(location) & {"element_path", "namespaces", "attributes"}:
        raise ValueError("XML positions require an XML source")
    if "text_role" in location and (
        kind not in {"html", "xml"} or location["text_role"] not in {"text", "tail"}
    ):
        raise ValueError("Invalid markup text role")
    if kind in {"html", "xml"} and set(location) & {"line", "line_end"}:
        raise ValueError("Markup tree coordinates are not conversion lines")
