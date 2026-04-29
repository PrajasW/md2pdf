---
title: YAML Sample Title
author:
  - YAML Author One
  - YAML Author Two
date: 2026-04-29
---

# Overview

This sample document exercises `mpdf` with headings, code, math, tables, and an image.

## Figure

![Bundled sample figure](example-figure.eps)

## Math

Inline math works, for example $e^{i\pi} + 1 = 0$.

Display math works too:

$$
\int_0^1 x^2 \, dx = \frac{1}{3}
$$

## Code

```python
from pathlib import Path

def render_markdown(path: str) -> Path:
    return Path(path).with_suffix(".pdf")

print(render_markdown("report.md"))
```

## Table

| Feature | Status |
| --- | --- |
| YAML metadata | Preserved |
| CLI overrides | Supported |
| XeLaTeX math | Supported |

## Next Steps

Run `mpdf example.md` to create `example.pdf` beside this file.
