"""Extract browser-image and sprite metadata from uploaded source projects.

The source importer deliberately keeps binary assets separate from source text.  This
module joins the two views without executing untrusted JavaScript: image dimensions
come from Pillow headers, while known sprite/atlas declarations are parsed
conservatively from source text.
"""

from __future__ import annotations

import io
import posixpath
import re
from collections.abc import Callable, Iterable
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError


SOURCE_ASSET_METADATA_VERSION = 3
SOURCE_ASSET_METADATA_KEYS = (
    "width",
    "height",
    "layout_type",
    "frame_width",
    "frame_height",
    "columns",
    "rows",
    "frame_count",
    "atlas_frames",
    "asset_role",
    "metadata_source",
)

_MAX_DIMENSION = 100_000
_MAX_ATLAS_FRAMES = 500
_IMPACT_SHEET_RE = re.compile(
    r"new\s+ig\.AnimationSheet\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"\r\n]+)(?P=quote)\s*,\s*"
    r"(?P<frame_width>\d+)\s*,\s*(?P<frame_height>\d+)",
    re.IGNORECASE,
)
_IMPACT_IMAGE_PROPERTY_RE = re.compile(
    r"\b(?P<property>[A-Za-z_$][\w$]*)\s*:\s*"
    r"new\s+ig\.Image\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"\r\n]+)(?P=quote)\s*\)",
    re.IGNORECASE,
)
_DRAW_TILE_RE = re.compile(
    r"\b(?:this\s*\.\s*)?(?P<property>[A-Za-z_$][\w$]*)"
    r"\s*\.\s*drawTile\s*\(",
    re.IGNORECASE,
)
_IMPACT_EXTEND_BLOCK_RE = re.compile(
    r"\big\.[A-Za-z_$][\w$]*\.extend\s*\(\s*\{",
    re.IGNORECASE,
)
_DEVELOPER_BRANDING_RE = re.compile(r"\bDeveloperBranding\b", re.IGNORECASE)
_SCOPE_BOUNDARY_RE = re.compile(
    r"\big\.module\s*\(|\big\.[A-Za-z_$][\w$]*\.extend\s*\(",
    re.IGNORECASE,
)
_INTEGER_LITERAL_RE = re.compile(r"^\s*(?P<value>\d+)(?:\.0+)?\s*$")
_FRAMES_RE = re.compile(r"\bframes\s*:\s*\{", re.IGNORECASE)
_META_RE = re.compile(r"\bmeta\s*:\s*\{", re.IGNORECASE)
_IMAGE_PROP_RE = re.compile(
    r"\b(?P<key>image(?:Big|bw)?)\s*:\s*"
    r"(?P<quote>['\"])(?P<path>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def _normalise_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    if not value:
        return ""
    normalised = posixpath.normpath(value)
    return "" if normalised in {"", "."} else normalised.lstrip("/")


def _clean_reference(value: str) -> str:
    value = (value or "").strip()
    if not value or value.startswith(("data:", "blob:", "//")):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return ""
    return _normalise_path(unquote(parsed.path))


def _resolve_asset(
    reference: str,
    source_path: str,
    assets_by_path: dict[str, dict],
) -> dict | None:
    cleaned = _clean_reference(reference)
    if not cleaned:
        return None
    source_dir = posixpath.dirname(_normalise_path(source_path))
    candidates = []
    if source_dir:
        candidates.append(_normalise_path(posixpath.join(source_dir, cleaned)))
    candidates.append(cleaned)
    for candidate in candidates:
        asset = assets_by_path.get(candidate.lower())
        if asset is not None:
            return asset

    # Source bundles commonly contain one leading project folder while engine
    # declarations are project-root relative.  A unique suffix is safe and useful,
    # but a bare basename is too ambiguous (many projects contain card.png/icon.png).
    if "/" not in cleaned:
        return None
    suffix = "/" + cleaned.lower().lstrip("/")
    matches = [asset for path, asset in assets_by_path.items() if path.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _image_dimensions(raw: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
        return None
    return int(width), int(height)


def _looks_like_placeholder_image(
    raw: bytes,
    path: str,
    dimensions: tuple[int, int],
) -> bool:
    """Detect large near-uniform mock backgrounds with a small text label.

    Source packages often ship ``Background 640x480`` placeholder JPEGs next to the
    real playfield.  Their filenames look legitimate, so path-only filtering is not
    enough.  Keep the heuristic deliberately narrow: a background/placeholder path,
    a sizeable image, almost entirely grayscale pixels, and one overwhelmingly light
    quantised colour.  Real illustrated backgrounds fail these colour constraints.
    """
    normalised = _normalise_path(path).lower()
    if not any(token in normalised for token in ("background", "placeholder")):
        return False
    width, height = dimensions
    if width * height < 100_000:
        return False
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((96, 96))
            packed = image.tobytes()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return False
    if len(packed) < 3:
        return False
    pixels = len(packed) // 3
    buckets: dict[tuple[int, int, int], int] = {}
    grayscale = 0
    light = 0
    for index in range(0, pixels * 3, 3):
        red, green, blue = packed[index:index + 3]
        bucket = (red // 16, green // 16, blue // 16)
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if max(red, green, blue) - min(red, green, blue) <= 10:
            grayscale += 1
        if (red + green + blue) / 3 >= 210:
            light += 1
    dominant_ratio = max(buckets.values(), default=0) / pixels
    return (
        dominant_ratio >= 0.90
        and grayscale / pixels >= 0.98
        and light / pixels >= 0.90
    )


def _find_matching_delimiter(
    text: str,
    open_index: int,
    opening: str,
    closing: str,
) -> int | None:
    """Return a matching JS delimiter while ignoring strings and comments."""
    if open_index < 0 or open_index >= len(text) or text[open_index] != opening:
        return None
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = open_index
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _find_matching_brace(text: str, open_index: int) -> int | None:
    """Return the matching ``}`` while ignoring strings and JS comments."""
    return _find_matching_delimiter(text, open_index, "{", "}")


def _find_matching_parenthesis(text: str, open_index: int) -> int | None:
    """Return the matching ``)`` while ignoring strings and JS comments."""
    return _find_matching_delimiter(text, open_index, "(", ")")


def _split_top_level_arguments(body: str) -> list[str]:
    """Split a JavaScript call body without splitting nested expressions."""
    arguments: list[str] = []
    stack: list[str] = []
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    start = 0
    index = 0
    matching = {")": "(", "]": "[", "}": "{"}
    while index < len(body):
        char = body[index]
        nxt = body[index + 1] if index + 1 < len(body) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == matching[char]:
                stack.pop()
        elif char == "," and not stack:
            arguments.append(body[start:index].strip())
            start = index + 1
        index += 1
    arguments.append(body[start:].strip())
    return arguments


def _object_properties(body: str) -> Iterable[tuple[str, str]]:
    """Yield top-level ``name: { ... }`` properties from an object body."""
    index = 0
    while index < len(body):
        while index < len(body) and (body[index].isspace() or body[index] == ","):
            index += 1
        if index >= len(body):
            return
        if body[index] in {"'", '"'}:
            quote = body[index]
            index += 1
            start = index
            escaped = False
            while index < len(body):
                if escaped:
                    escaped = False
                elif body[index] == "\\":
                    escaped = True
                elif body[index] == quote:
                    break
                index += 1
            name = body[start:index]
            index += 1
        else:
            match = re.match(r"[A-Za-z_$][\w$-]*", body[index:])
            if not match:
                index += 1
                continue
            name = match.group(0)
            index += len(name)
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body) or body[index] != ":":
            continue
        index += 1
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body) or body[index] != "{":
            # Only nested objects can be atlas frame records.  Skip primitive values.
            while index < len(body) and body[index] != ",":
                index += 1
            continue
        end = _find_matching_brace(body, index)
        if end is None:
            return
        yield name, body[index + 1:end]
        index = end + 1


def _rect_from_property(body: str, property_name: str) -> dict[str, int] | None:
    match = re.search(rf"\b{re.escape(property_name)}\s*:\s*\{{", body)
    if not match:
        return None
    open_index = body.find("{", match.start())
    end = _find_matching_brace(body, open_index)
    if end is None:
        return None
    rect_body = body[open_index + 1:end]
    values: dict[str, int] = {}
    for key in ("x", "y", "w", "h"):
        value_match = re.search(rf"\b{key}\s*:\s*(-?\d+)", rect_body)
        if not value_match:
            return None
        values[key] = int(value_match.group(1))
    if values["x"] < 0 or values["y"] < 0 or values["w"] <= 0 or values["h"] <= 0:
        return None
    return values


def _rect_within_image(rect: dict[str, int], asset: dict) -> bool:
    width = asset.get("width")
    height = asset.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return True
    return rect["x"] + rect["w"] <= width and rect["y"] + rect["h"] <= height


def _is_explicit_branding_path(path: str) -> bool:
    """Recognise only unambiguous branding directory names."""
    segments = {
        segment.lower()
        for segment in _normalise_path(path).split("/")
        if segment
    }
    return bool(segments & {
        "branding",
        "developer-branding",
        "developer_branding",
        "developerbranding",
    })


def _developer_branding_blocks(content: str) -> list[tuple[int, int]]:
    """Return bounded ImpactJS entity/class blocks gated by DeveloperBranding."""
    spans: list[tuple[int, int]] = []
    for match in _IMPACT_EXTEND_BLOCK_RE.finditer(content):
        open_index = content.find("{", match.start(), match.end())
        end = _find_matching_brace(content, open_index)
        if end is None:
            continue
        if _DEVELOPER_BRANDING_RE.search(content, open_index, end + 1):
            spans.append((open_index, end + 1))
    return spans


def _has_developer_branding_context(
    content: str,
    declaration_index: int,
    branded_blocks: list[tuple[int, int]],
) -> bool:
    if any(start <= declaration_index < end for start, end in branded_blocks):
        return True

    # Some uploaded snippets contain only a partial entity literal.  Accept a close
    # DeveloperBranding reference only when no module/class boundary separates it
    # from the image declaration.  This avoids tagging a normal menu title merely
    # because a different branding entity appears nearby in the same bundled file.
    window_start = max(0, declaration_index - 2048)
    window_end = min(len(content), declaration_index + 2048)
    for match in _DEVELOPER_BRANDING_RE.finditer(content, window_start, window_end):
        between = content[
            min(declaration_index, match.start()):max(declaration_index, match.end())
        ]
        if not _SCOPE_BOUNDARY_RE.search(between):
            return True
    return False


def _positive_integer_literal(value: str) -> int | None:
    match = _INTEGER_LITERAL_RE.fullmatch(value)
    if not match:
        return None
    result = int(match.group("value"))
    return result if 0 < result <= _MAX_DIMENSION else None


def _apply_impact_image_tiles(
    source_files: list[dict],
    assets_by_path: dict[str, dict],
) -> None:
    """Join ``prop: new ig.Image(path)`` declarations to ``prop.drawTile`` calls."""
    for source in source_files:
        content = str(source.get("content") or "")
        source_path = str(source.get("path") or "")
        branded_blocks = _developer_branding_blocks(content)
        declarations: dict[str, list[tuple[int, dict]]] = {}

        for match in _IMPACT_IMAGE_PROPERTY_RE.finditer(content):
            asset = _resolve_asset(match.group("path"), source_path, assets_by_path)
            if asset is None:
                continue
            property_name = match.group("property").lower()
            declarations.setdefault(property_name, []).append((match.start(), asset))
            if (
                _is_explicit_branding_path(str(asset.get("path") or ""))
                or _has_developer_branding_context(
                    content,
                    match.start(),
                    branded_blocks,
                )
            ):
                asset["asset_role"] = "developer_branding"

        for draw_match in _DRAW_TILE_RE.finditer(content):
            candidates = declarations.get(draw_match.group("property").lower()) or []
            candidates = [item for item in candidates if item[0] <= draw_match.start()]
            if not candidates:
                continue
            _, asset = max(candidates, key=lambda item: item[0])
            if asset.get("layout_type") in {"atlas", "sprite_sheet"}:
                continue

            open_index = content.rfind("(", draw_match.start(), draw_match.end())
            close_index = _find_matching_parenthesis(content, open_index)
            if close_index is None:
                continue
            arguments = _split_top_level_arguments(content[open_index + 1:close_index])
            if len(arguments) < 5:
                continue
            frame_width = _positive_integer_literal(arguments[3])
            frame_height = _positive_integer_literal(arguments[4])
            width = asset.get("width")
            height = asset.get("height")
            if not (
                frame_width
                and frame_height
                and isinstance(width, int)
                and isinstance(height, int)
                and width % frame_width == 0
                and height % frame_height == 0
            ):
                continue
            columns = width // frame_width
            rows = height // frame_height
            if columns * rows <= 1:
                continue
            asset["layout_type"] = "sprite_sheet"
            asset["frame_width"] = frame_width
            asset["frame_height"] = frame_height
            asset["columns"] = columns
            asset["rows"] = rows
            asset["frame_count"] = columns * rows
            line = content.count("\n", 0, draw_match.start()) + 1
            asset["metadata_source"] = f"impactjs-drawtile:{source_path}:{line}"


def _apply_impact_sheets(
    source_files: list[dict],
    assets_by_path: dict[str, dict],
) -> None:
    for source in source_files:
        content = str(source.get("content") or "")
        source_path = str(source.get("path") or "")
        for match in _IMPACT_SHEET_RE.finditer(content):
            asset = _resolve_asset(match.group("path"), source_path, assets_by_path)
            if asset is None:
                continue
            frame_width = int(match.group("frame_width"))
            frame_height = int(match.group("frame_height"))
            if not (
                0 < frame_width <= _MAX_DIMENSION
                and 0 < frame_height <= _MAX_DIMENSION
            ):
                continue
            asset["layout_type"] = "sprite_sheet"
            asset["frame_width"] = frame_width
            asset["frame_height"] = frame_height
            width = asset.get("width")
            height = asset.get("height")
            if isinstance(width, int) and width >= frame_width:
                asset["columns"] = width // frame_width
            if isinstance(height, int) and height >= frame_height:
                asset["rows"] = height // frame_height
            if isinstance(asset.get("columns"), int) and isinstance(asset.get("rows"), int):
                asset["frame_count"] = asset["columns"] * asset["rows"]
            line = content.count("\n", 0, match.start()) + 1
            asset["metadata_source"] = f"impactjs:{source_path}:{line}"


def _apply_javascript_atlases(
    source_files: list[dict],
    assets_by_path: dict[str, dict],
) -> None:
    for source in source_files:
        content = str(source.get("content") or "")
        source_path = str(source.get("path") or "")
        for frames_match in _FRAMES_RE.finditer(content):
            frames_open = content.find("{", frames_match.start())
            frames_end = _find_matching_brace(content, frames_open)
            if frames_end is None:
                continue
            # In TexturePacker-style literals meta immediately follows frames.  Keep
            # this window bounded so an unrelated later meta object is never joined.
            meta_match = _META_RE.search(content, frames_end + 1, min(len(content), frames_end + 4096))
            if not meta_match:
                continue
            meta_open = content.find("{", meta_match.start())
            meta_end = _find_matching_brace(content, meta_open)
            if meta_end is None:
                continue
            frame_records: dict[str, dict[str, dict[str, int]]] = {}
            frames_body = content[frames_open + 1:frames_end]
            for name, record_body in _object_properties(frames_body):
                if len(frame_records) >= _MAX_ATLAS_FRAMES or len(name) > 120:
                    break
                normal = _rect_from_property(record_body, "frame")
                large = _rect_from_property(record_body, "frameBig")
                if normal or large:
                    frame_records[name] = {"frame": normal, "frameBig": large}
            if not frame_records:
                continue
            meta_body = content[meta_open + 1:meta_end]
            for image_match in _IMAGE_PROP_RE.finditer(meta_body):
                asset = _resolve_asset(image_match.group("path"), source_path, assets_by_path)
                if asset is None:
                    continue
                property_name = image_match.group("key").lower()
                rect_key = "frameBig" if property_name == "imagebig" else "frame"
                atlas_frames = {
                    name: rects[rect_key]
                    for name, rects in frame_records.items()
                    if rects.get(rect_key) and _rect_within_image(rects[rect_key], asset)
                }
                if not atlas_frames:
                    continue
                asset["layout_type"] = "atlas"
                asset["atlas_frames"] = atlas_frames
                asset["frame_count"] = len(atlas_frames)
                line = content.count("\n", 0, frames_match.start()) + 1
                asset["metadata_source"] = f"atlas:{source_path}:{line}"


def enrich_source_asset_metadata(
    source_files: list[dict],
    assets: list[dict],
    *,
    content_loader: Callable[[dict], bytes | None] | None = None,
) -> bool:
    """Mutate source asset records with dimensions and source-derived layout data.

    ``asset["content"]`` is used for fresh uploads.  Legacy records can provide a
    ``content_loader`` that reads their already-persisted bundle file.  Invalid or
    unsupported images remain usable; they simply receive no dimensions.
    """
    before = [
        {key: asset.get(key) for key in SOURCE_ASSET_METADATA_KEYS if key in asset}
        for asset in assets
    ]
    for asset in assets:
        if asset.get("kind") != "image":
            continue
        raw = asset.get("content")
        if not isinstance(raw, bytes) and content_loader is not None:
            try:
                raw = content_loader(asset)
            except OSError:
                raw = None
        if isinstance(raw, bytes):
            dimensions = _image_dimensions(raw)
            if dimensions:
                asset["width"], asset["height"] = dimensions
                if _looks_like_placeholder_image(
                    raw,
                    str(asset.get("path") or ""),
                    dimensions,
                ):
                    asset["asset_role"] = "placeholder"

    assets_by_path = {
        _normalise_path(str(asset.get("path") or "")).lower(): asset
        for asset in assets
        if asset.get("path")
    }
    for asset in assets:
        if _is_explicit_branding_path(str(asset.get("path") or "")):
            asset["asset_role"] = "developer_branding"
    _apply_impact_sheets(source_files, assets_by_path)
    _apply_impact_image_tiles(source_files, assets_by_path)
    _apply_javascript_atlases(source_files, assets_by_path)
    after = [
        {key: asset.get(key) for key in SOURCE_ASSET_METADATA_KEYS if key in asset}
        for asset in assets
    ]
    return before != after
