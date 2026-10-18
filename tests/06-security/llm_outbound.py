#!/usr/bin/env python3
import json
import re
from pathlib import Path

SECRET_PATTERNS = re.compile(
    r"passw(?:or|d)|secret|token|api[_-]?key|auth|private[_-]?key", re.IGNORECASE)
