"""
SVG colors.

"""

import re

COLORS = {
    'aliceblue': (240 / 255, 248 / 255, 255 / 255, 1),
    'antiquewhite': (250 / 255, 235 / 255, 215 / 255, 1),
    'aqua': (0 / 255, 255 / 255, 255 / 255, 1),
    'aquamarine': (127 / 255, 255 / 255, 212 / 255, 1),
    'azure': (240 / 255, 255 / 255, 255 / 255, 1),
    'beige': (245 / 255, 245 / 255, 220 / 255, 1),
    'bisque': (255 / 255, 228 / 255, 196 / 255, 1),
    'black': (0 / 255, 0 / 255, 0 / 255, 1),
    'blanchedalmond': (255 / 255, 235 / 255, 205 / 255, 1),
    'blue': (0 / 255, 0 / 255, 255 / 255, 1),
    'blueviolet': (138 / 255, 43 / 255, 226 / 255, 1),
    'brown': (165 / 255, 42 / 255, 42 / 255, 1),
    'burlywood': (222 / 255, 184 / 255, 135 / 255, 1),
    'cadetblue': (95 / 255, 158 / 255, 160 / 255, 1),
    'chartreuse': (127 / 255, 255 / 255, 0 / 255, 1),
    'chocolate': (210 / 255, 105 / 255, 30 / 255, 1),
    'coral': (255 / 255, 127 / 255, 80 / 255, 1),
    'cornflowerblue': (100 / 255, 149 / 255, 237 / 255, 1),
    'cornsilk': (255 / 255, 248 / 255, 220 / 255, 1),
    'crimson': (220 / 255, 20 / 255, 60 / 255, 1),
    'cyan': (0 / 255, 255 / 255, 255 / 255, 1),
    'darkblue': (0 / 255, 0 / 255, 139 / 255, 1),
    'darkcyan': (0 / 255, 139 / 255, 139 / 255, 1),
    'darkgoldenrod': (184 / 255, 134 / 255, 11 / 255, 1),
    'darkgray': (169 / 255, 169 / 255, 169 / 255, 1),
    'darkgreen': (0 / 255, 100 / 255, 0 / 255, 1),
    'darkgrey': (169 / 255, 169 / 255, 169 / 255, 1),
    'darkkhaki': (189 / 255, 183 / 255, 107 / 255, 1),
    'darkmagenta': (139 / 255, 0 / 255, 139 / 255, 1),
    'darkolivegreen': (85 / 255, 107 / 255, 47 / 255, 1),
    'darkorange': (255 / 255, 140 / 255, 0 / 255, 1),
    'darkorchid': (153 / 255, 50 / 255, 204 / 255, 1),
    'darkred': (139 / 255, 0 / 255, 0 / 255, 1),
    'darksalmon': (233 / 255, 150 / 255, 122 / 255, 1),
    'darkseagreen': (143 / 255, 188 / 255, 143 / 255, 1),
    'darkslateblue': (72 / 255, 61 / 255, 139 / 255, 1),
    'darkslategray': (47 / 255, 79 / 255, 79 / 255, 1),
    'darkslategrey': (47 / 255, 79 / 255, 79 / 255, 1),
    'darkturquoise': (0 / 255, 206 / 255, 209 / 255, 1),
    'darkviolet': (148 / 255, 0 / 255, 211 / 255, 1),
    'deeppink': (255 / 255, 20 / 255, 147 / 255, 1),
    'deepskyblue': (0 / 255, 191 / 255, 255 / 255, 1),
    'dimgray': (105 / 255, 105 / 255, 105 / 255, 1),
    'dimgrey': (105 / 255, 105 / 255, 105 / 255, 1),
    'dodgerblue': (30 / 255, 144 / 255, 255 / 255, 1),
    'firebrick': (178 / 255, 34 / 255, 34 / 255, 1),
    'floralwhite': (255 / 255, 250 / 255, 240 / 255, 1),
    'forestgreen': (34 / 255, 139 / 255, 34 / 255, 1),
    'fuchsia': (255 / 255, 0 / 255, 255 / 255, 1),
    'gainsboro': (220 / 255, 220 / 255, 220 / 255, 1),
    'ghostwhite': (248 / 255, 248 / 255, 255 / 255, 1),
    'gold': (255 / 255, 215 / 255, 0 / 255, 1),
    'goldenrod': (218 / 255, 165 / 255, 32 / 255, 1),
    'gray': (128 / 255, 128 / 255, 128 / 255, 1),
    'grey': (128 / 255, 128 / 255, 128 / 255, 1),
    'green': (0 / 255, 128 / 255, 0 / 255, 1),
    'greenyellow': (173 / 255, 255 / 255, 47 / 255, 1),
    'honeydew': (240 / 255, 255 / 255, 240 / 255, 1),
    'hotpink': (255 / 255, 105 / 255, 180 / 255, 1),
    'indianred': (205 / 255, 92 / 255, 92 / 255, 1),
    'indigo': (75 / 255, 0 / 255, 130 / 255, 1),
    'ivory': (255 / 255, 255 / 255, 240 / 255, 1),
    'khaki': (240 / 255, 230 / 255, 140 / 255, 1),
    'lavender': (230 / 255, 230 / 255, 250 / 255, 1),
    'lavenderblush': (255 / 255, 240 / 255, 245 / 255, 1),
    'lawngreen': (124 / 255, 252 / 255, 0 / 255, 1),
    'lemonchiffon': (255 / 255, 250 / 255, 205 / 255, 1),
    'lightblue': (173 / 255, 216 / 255, 230 / 255, 1),
    'lightcoral': (240 / 255, 128 / 255, 128 / 255, 1),
    'lightcyan': (224 / 255, 255 / 255, 255 / 255, 1),
    'lightgoldenrodyellow': (250 / 255, 250 / 255, 210 / 255, 1),
    'lightgray': (211 / 255, 211 / 255, 211 / 255, 1),
    'lightgreen': (144 / 255, 238 / 255, 144 / 255, 1),
    'lightgrey': (211 / 255, 211 / 255, 211 / 255, 1),
    'lightpink': (255 / 255, 182 / 255, 193 / 255, 1),
    'lightsalmon': (255 / 255, 160 / 255, 122 / 255, 1),
    'lightseagreen': (32 / 255, 178 / 255, 170 / 255, 1),
    'lightskyblue': (135 / 255, 206 / 255, 250 / 255, 1),
    'lightslategray': (119 / 255, 136 / 255, 153 / 255, 1),
    'lightslategrey': (119 / 255, 136 / 255, 153 / 255, 1),
    'lightsteelblue': (176 / 255, 196 / 255, 222 / 255, 1),
    'lightyellow': (255 / 255, 255 / 255, 224 / 255, 1),
    'lime': (0 / 255, 255 / 255, 0 / 255, 1),
    'limegreen': (50 / 255, 205 / 255, 50 / 255, 1),
    'linen': (250 / 255, 240 / 255, 230 / 255, 1),
    'magenta': (255 / 255, 0 / 255, 255 / 255, 1),
    'maroon': (128 / 255, 0 / 255, 0 / 255, 1),
    'mediumaquamarine': (102 / 255, 205 / 255, 170 / 255, 1),
    'mediumblue': (0 / 255, 0 / 255, 205 / 255, 1),
    'mediumorchid': (186 / 255, 85 / 255, 211 / 255, 1),
    'mediumpurple': (147 / 255, 112 / 255, 219 / 255, 1),
    'mediumseagreen': (60 / 255, 179 / 255, 113 / 255, 1),
    'mediumslateblue': (123 / 255, 104 / 255, 238 / 255, 1),
    'mediumspringgreen': (0 / 255, 250 / 255, 154 / 255, 1),
    'mediumturquoise': (72 / 255, 209 / 255, 204 / 255, 1),
    'mediumvioletred': (199 / 255, 21 / 255, 133 / 255, 1),
    'midnightblue': (25 / 255, 25 / 255, 112 / 255, 1),
    'mintcream': (245 / 255, 255 / 255, 250 / 255, 1),
    'mistyrose': (255 / 255, 228 / 255, 225 / 255, 1),
    'moccasin': (255 / 255, 228 / 255, 181 / 255, 1),
    'navajowhite': (255 / 255, 222 / 255, 173 / 255, 1),
    'navy': (0 / 255, 0 / 255, 128 / 255, 1),
    'oldlace': (253 / 255, 245 / 255, 230 / 255, 1),
    'olive': (128 / 255, 128 / 255, 0 / 255, 1),
    'olivedrab': (107 / 255, 142 / 255, 35 / 255, 1),
    'orange': (255 / 255, 165 / 255, 0 / 255, 1),
    'orangered': (255 / 255, 69 / 255, 0 / 255, 1),
    'orchid': (218 / 255, 112 / 255, 214 / 255, 1),
    'palegoldenrod': (238 / 255, 232 / 255, 170 / 255, 1),
    'palegreen': (152 / 255, 251 / 255, 152 / 255, 1),
    'paleturquoise': (175 / 255, 238 / 255, 238 / 255, 1),
    'palevioletred': (219 / 255, 112 / 255, 147 / 255, 1),
    'papayawhip': (255 / 255, 239 / 255, 213 / 255, 1),
    'peachpuff': (255 / 255, 218 / 255, 185 / 255, 1),
    'peru': (205 / 255, 133 / 255, 63 / 255, 1),
    'pink': (255 / 255, 192 / 255, 203 / 255, 1),
    'plum': (221 / 255, 160 / 255, 221 / 255, 1),
    'powderblue': (176 / 255, 224 / 255, 230 / 255, 1),
    'purple': (128 / 255, 0 / 255, 128 / 255, 1),
    'red': (255 / 255, 0 / 255, 0 / 255, 1),
    'rosybrown': (188 / 255, 143 / 255, 143 / 255, 1),
    'royalblue': (65 / 255, 105 / 255, 225 / 255, 1),
    'saddlebrown': (139 / 255, 69 / 255, 19 / 255, 1),
    'salmon': (250 / 255, 128 / 255, 114 / 255, 1),
    'sandybrown': (244 / 255, 164 / 255, 96 / 255, 1),
    'seagreen': (46 / 255, 139 / 255, 87 / 255, 1),
    'seashell': (255 / 255, 245 / 255, 238 / 255, 1),
    'sienna': (160 / 255, 82 / 255, 45 / 255, 1),
    'silver': (192 / 255, 192 / 255, 192 / 255, 1),
    'skyblue': (135 / 255, 206 / 255, 235 / 255, 1),
    'slateblue': (106 / 255, 90 / 255, 205 / 255, 1),
    'slategray': (112 / 255, 128 / 255, 144 / 255, 1),
    'slategrey': (112 / 255, 128 / 255, 144 / 255, 1),
    'snow': (255 / 255, 250 / 255, 250 / 255, 1),
    'springgreen': (0 / 255, 255 / 255, 127 / 255, 1),
    'steelblue': (70 / 255, 130 / 255, 180 / 255, 1),
    'tan': (210 / 255, 180 / 255, 140 / 255, 1),
    'teal': (0 / 255, 128 / 255, 128 / 255, 1),
    'thistle': (216 / 255, 191 / 255, 216 / 255, 1),
    'tomato': (255 / 255, 99 / 255, 71 / 255, 1),
    'turquoise': (64 / 255, 224 / 255, 208 / 255, 1),
    'violet': (238 / 255, 130 / 255, 238 / 255, 1),
    'wheat': (245 / 255, 222 / 255, 179 / 255, 1),
    'white': (255 / 255, 255 / 255, 255 / 255, 1),
    'whitesmoke': (245 / 255, 245 / 255, 245 / 255, 1),
    'yellow': (255 / 255, 255 / 255, 0 / 255, 1),
    'yellowgreen': (154 / 255, 205 / 255, 50 / 255, 1),

    'activeborder': (0, 0, 1, 1),
    'activecaption': (0, 0, 1, 1),
    'appworkspace': (1, 1, 1, 1),
    'background': (1, 1, 1, 1),
    'buttonface': (0, 0, 0, 1),
    'buttonhighlight': (0.8, 0.8, 0.8, 1),
    'buttonshadow': (0.2, 0.2, 0.2, 1),
    'buttontext': (0, 0, 0, 1),
    'captiontext': (0, 0, 0, 1),
    'graytext': (0.2, 0.2, 0.2, 1),
    'highlight': (0, 0, 1, 1),
    'highlighttext': (0.8, 0.8, 0.8, 1),
    'inactiveborder': (0.2, 0.2, 0.2, 1),
    'inactivecaption': (0.8, 0.8, 0.8, 1),
    'inactivecaptiontext': (0.2, 0.2, 0.2, 1),
    'infobackground': (0.8, 0.8, 0.8, 1),
    'infotext': (0, 0, 0, 1),
    'menu': (0.8, 0.8, 0.8, 1),
    'menutext': (0.2, 0.2, 0.2, 1),
    'scrollbar': (0.8, 0.8, 0.8, 1),
    'threeddarkshadow': (0.2, 0.2, 0.2, 1),
    'threedface': (0.8, 0.8, 0.8, 1),
    'threedhighlight': (1, 1, 1, 1),
    'threedlightshadow': (0.2, 0.2, 0.2, 1),
    'threedshadow': (0.2, 0.2, 0.2, 1),
    'window': (0.8, 0.8, 0.8, 1),
    'windowframe': (0.8, 0.8, 0.8, 1),
    'windowtext': (0, 0, 0, 1),

    'none': (0, 0, 0, 0),
    'transparent': (0, 0, 0, 0),
}

# Fallback returned for invalid colors, kept for backward compatibility:
# unrecognized values paint as opaque black instead of aborting the document.
INVALID_COLOR = (0, 0, 0, 1)

# CSS Color 4 "none" keyword is not supported, it falls back to INVALID_COLOR.
# Hex colors: #RGB, #RGBA, #RRGGBB and #RRGGBBAA, full match only.
HEX_COLOR = re.compile(r'#([0-9a-f]{3,8})\Z')

# rgb()/rgba() functional notation. The body grammar is checked explicitly
# below, this regex only isolates the argument list.
RGB_FUNCTION = re.compile(r'rgba?\((.*)\)\Z', re.DOTALL)

_NUMBER = r'[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?'
_NUMBER_TOKEN = re.compile(rf'({_NUMBER})(%?)\Z', re.IGNORECASE)


def _parse_channel(token):
    """Parse one RGB channel token, returning ``(value, is_percentage)``.

    ``value`` is normalized to the [0, 1] range (clamped). Number channels
    are divided by 255, percentage channels by 100.

    """
    match = _NUMBER_TOKEN.match(token.strip())
    if not match:
        raise ValueError(f'invalid channel: {token!r}')
    number = float(match.group(1))
    if match.group(2):
        value = number / 100
    else:
        value = number / 255
    return min(1, max(0, value)), bool(match.group(2))


def _parse_alpha(token):
    """Parse an alpha channel token into a clamped [0, 1] value."""
    match = _NUMBER_TOKEN.match(token.strip())
    if not match:
        raise ValueError(f'invalid alpha: {token!r}')
    number = float(match.group(1))
    # Alpha percentages are divided by 100; bare numbers are already factors.
    value = number / 100 if match.group(2) else number
    return min(1, max(0, value))


def _parse_rgb_function(body):
    """Parse the body of an ``rgb()`` / ``rgba()`` function.

    Supports both the legacy comma-separated syntax and the CSS Color 4
    space-separated syntax with a slash introducing the alpha value.

    """
    body = body.strip()
    if not body:
        raise ValueError('empty rgb function')

    if ',' in body:
        # Legacy syntax: channels and alpha separated by commas. A slash is
        # not allowed in legacy syntax, and every separator must be a comma.
        if '/' in body:
            raise ValueError('slash is invalid in comma-separated rgb()')
        tokens = [token.strip() for token in body.split(',')]
        if not 3 <= len(tokens) <= 4:
            raise ValueError(
                f'rgb() expects 3 or 4 components, got {len(tokens)}')
        channels = [_parse_channel(token) for token in tokens[:3]]
        alpha = _parse_alpha(tokens[3]) if len(tokens) == 4 else 1
    else:
        # Modern syntax: channels separated by whitespace, optional
        # " / <alpha>" suffix.
        if '/' in body:
            rgb_part, alpha_part = body.split('/', 1)
            alpha = _parse_alpha(alpha_part)
        else:
            rgb_part, alpha = body, 1
        tokens = rgb_part.split()
        if len(tokens) != 3:
            raise ValueError(
                f'rgb() expects 3 channels, got {len(tokens)}')
        channels = [_parse_channel(token) for token in tokens]

    # Mixing number and percentage channels is invalid (CSS Color 4 §4.5).
    is_percentage = channels[0][1]
    if any(flag != is_percentage for _, flag in channels):
        raise ValueError('number and percentage channels cannot be mixed')

    r, g, b = (value for value, _ in channels)
    return r, g, b, alpha


def color(string, opacity=1):
    """Replace ``string`` representing a color by a RGBA tuple.

    Supported notations:
      - SVG/CSS named colors, ``none`` and ``transparent``;
      - ``#RGB``, ``#RGBA``, ``#RRGGBB`` and ``#RRGGBBAA`` hex colors, the
        extra hex digits encoding the color alpha channel;
      - ``rgb()`` and ``rgba()`` in the legacy comma-separated syntax, with
        number or percentage channels and a number/percentage alpha;
      - the CSS Color 4 space-separated syntax, including
        ``rgb(R G B / A)`` and percentage alpha values.

    The color's own alpha channel is combined with the external ``opacity``
    by multiplication: callers pass fill-opacity, stroke-opacity,
    stop-opacity or group opacity here, so it must not be applied again.

    Invalid colors return :data:`INVALID_COLOR` (opaque black), matching the
    project's historical behavior: one bad value never prevents the rest of
    the document from rendering.

    See http://www.w3.org/TR/SVG/types.html#DataTypeColor and
    https://www.w3.org/TR/css-color-4/#rgb-functions

    """
    if not string:
        return (0, 0, 0, 0)

    string = string.strip().lower()

    if string in COLORS:
        r, g, b, alpha = COLORS[string]
        return (r, g, b, alpha * opacity)

    try:
        match = HEX_COLOR.match(string)
        if match:
            digits = match.group(1)
            if len(digits) == 3:
                r = int(digits[0] * 2, 16) / 255
                g = int(digits[1] * 2, 16) / 255
                b = int(digits[2] * 2, 16) / 255
                alpha = 1
            elif len(digits) == 4:
                r = int(digits[0] * 2, 16) / 255
                g = int(digits[1] * 2, 16) / 255
                b = int(digits[2] * 2, 16) / 255
                alpha = int(digits[3] * 2, 16) / 255
            elif len(digits) == 6:
                r = int(digits[0:2], 16) / 255
                g = int(digits[2:4], 16) / 255
                b = int(digits[4:6], 16) / 255
                alpha = 1
            elif len(digits) == 8:
                r = int(digits[0:2], 16) / 255
                g = int(digits[2:4], 16) / 255
                b = int(digits[4:6], 16) / 255
                alpha = int(digits[6:8], 16) / 255
            else:  # pragma: no cover - excluded by the regex
                raise ValueError(f'invalid hex color: {string!r}')
            return (r, g, b, alpha * opacity)

        match = RGB_FUNCTION.match(string)
        if match:
            r, g, b, alpha = _parse_rgb_function(match.group(1))
            return (r, g, b, alpha * opacity)
    except (ValueError, OverflowError):
        # Malformed color: keep the historical fallback instead of raising.
        return INVALID_COLOR

    return INVALID_COLOR


def negate_color(rgba_tuple):
    """Replace ``rgba_tuple`` with its complementary color."""
    r, g, b, a = rgba_tuple
    return (1 - r, 1 - g, 1 - b, a)
