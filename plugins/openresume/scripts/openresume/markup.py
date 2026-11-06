"""Small, strict inline markup shared by the document renderers."""


def parse_markup(text):
    r"""Return (character, bold override, italic override) for \b{} and \i{}."""
    if not isinstance(text, str) or any(c in text for c in "\r\n\t"):
        raise ValueError("Edit text must be a string without line breaks or tabs.")
    result = []
    stack = []
    bold = italic = None
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\":
            if index + 1 < len(text) and text[index + 1] in "\\{}":
                result.append((text[index + 1], bold, italic))
                index += 2
                continue
            if text[index:index + 3] in ("\\b{", "\\i{"):
                if len(stack) >= 32:
                    raise ValueError("Inline formatting is nested too deeply.")
                stack.append((bold, italic))
                if text[index + 1] == "b":
                    bold = True
                else:
                    italic = True
                index += 3
                continue
            raise ValueError("Unknown inline markup. Use \\b{...}, \\i{...}, or escape a literal backslash.")
        if character == "}":
            if not stack:
                raise ValueError("Unmatched closing brace in inline markup.")
            bold, italic = stack.pop()
        elif character == "{":
            raise ValueError("Escape literal braces in inline markup.")
        else:
            result.append((character, bold, italic))
        index += 1
    if stack:
        raise ValueError("Unclosed inline formatting group.")
    if not any(character.strip() for character, *_ in result):
        raise ValueError("Edit text must be nonempty.")
    return result


def format_range(original, operation):
    """Validate a formatting-only edit and locate one exact occurrence."""
    characters = parse_markup(operation["text"])
    plain = "".join(character for character, *_ in characters)
    match = operation.get("match", original)
    if not isinstance(match, str) or not match or plain != match:
        raise ValueError("Formatting must preserve the matched text exactly.")
    starts = []
    start = 0
    while (start := original.find(match, start)) != -1:
        starts.append(start)
        start += len(match)
    occurrence = operation.get("occurrence")
    if occurrence is None:
        if len(starts) != 1:
            raise ValueError("Formatting match is missing or ambiguous; specify an occurrence.")
        occurrence = 1
    if type(occurrence) is not int or not 1 <= occurrence <= len(starts):
        raise ValueError("Invalid formatting match occurrence.")
    return starts[occurrence - 1], characters
