#!/bin/bash
# WHYSHU Partner PPT — Feishu Slides Build Script (2026-07-29 verified)
# Usage: node this_script.js
# Output: Feishu Slides JSON array → pipes to lark-cli slides +create --slides

# Colors: rgb(13,27,42) bg, rgb(0,200,150) teal, rgb(200,210,220) body, rgb(255,255,255) title
# Page: 960×540

B="rgb(200,210,220)"; T="rgb(0,200,150)"; W="rgb(255,255,255)"; BG="rgb(13,27,42)"; CD="rgb(22,32,48)"

build_slide() {
  # Each slide: <slide xmlns="http://www.larkoffice.com/sml/2.0"> ... </slide>
  # xmlns is REQUIRED — missing causes 3350001
  echo "$1"
}

# === TIPS ===
# - Text: <shape type="text"><content textType="body" fontSize="14" color="$B"><p>text</p></content></shape>
# - Card: <shape type="rect" radius="6"><fill><fillColor color="$CD"/></fill></shape>
# - Line: <line startX="80" startY="200" endX="880" endY="200"><border color="rgb(30,42,58)" width="1"/></line>
# - Badge: <shape type="rect" radius="4"><fill><fillColor color="$T"/></fill></shape><shape type="text" ...><content textType="caption" fontSize="10" color="$BG" bold="true"><p>运营</p></content></shape>
# - Remember: valign="middle" + margin:0 for text alignment
# - Use number circles instead of emoji on dark backgrounds
