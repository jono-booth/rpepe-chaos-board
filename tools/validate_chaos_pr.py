#!/usr/bin/env python3

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser


ALLOWED_CHANGED_FILES = {
    "chaos-board/index.html",
    "chaos-board/assets/chaos.css",
}

CHAOS_START = "<!-- CHAOS_START -->"
CHAOS_END = "<!-- CHAOS_END -->"


def run(cmd: list[str], *, text: bool = True) -> str:
    return subprocess.check_output(cmd, text=text, stderr=subprocess.STDOUT).strip()


def git(*args: str) -> str:
    return run(["git", *args])


def get_base_ref() -> str:
    # GitHub Actions sets GITHUB_BASE_REF for pull_request workflows.
    base = os.environ.get("GITHUB_BASE_REF")
    if base:
        return base
    # Fallback: infer from upstream HEAD.
    try:
        return git("rev-parse", "--abbrev-ref", "HEAD")
    except Exception:
        return "main"


def changed_files(base_ref: str) -> list[str]:
    # Compare against merge-base with base branch.
    git("fetch", "--no-tags", "--prune", "--depth=200", "origin", base_ref)
    merge_base = git("merge-base", "HEAD", f"origin/{base_ref}")
    names = git("diff", "--name-only", f"{merge_base}..HEAD")
    return [n for n in names.splitlines() if n.strip()]


def read_file_at_ref(ref: str, path: str) -> str:
    return run(["git", "show", f"{ref}:{path}"])


def read_worktree(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def split_chaos_region(html: str) -> tuple[str, str, str]:
    start = html.find(CHAOS_START)
    end = html.find(CHAOS_END)
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Missing or misordered chaos markers")
    before = html[: start + len(CHAOS_START)]
    region = html[start + len(CHAOS_START) : end]
    after = html[end:]
    return before, region, after


class ChaosHTMLSafetyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        # No inline event handlers.
        for k in list(attr_map.keys()):
            if k.startswith("on"):
                self.errors.append(f"Inline event handler attribute not allowed: {tag}[{k}]")

        if tag.lower() == "a":
            href = attr_map.get("href", "")
            if href:
                href_l = href.strip().lower()
                if href_l.startswith("javascript:"):
                    self.errors.append("Links must not use javascript: URLs")
                if not (href_l.startswith("http://") or href_l.startswith("https://") or href_l.startswith("#")):
                    self.errors.append(f"Links must be http(s) or fragment only: href={href}")

            # If it's an external link, enforce target+rel.
            if href.strip().lower().startswith(("http://", "https://")):
                if attr_map.get("target") != "_blank":
                    self.errors.append("External links must include target=\"_blank\"")
                rel = " ".join((attr_map.get("rel") or "").split()).lower()
                required = {"nofollow", "noopener", "noreferrer"}
                present = set(rel.split())
                if not required.issubset(present):
                    self.errors.append("External links must include rel=\"nofollow noopener noreferrer\"")

        if tag.lower() == "img":
            src = attr_map.get("src", "")
            if src and not src.strip().lower().startswith("https://"):
                self.errors.append("Images must use https:// sources")
            if "alt" not in attr_map or not attr_map.get("alt"):
                self.errors.append("Images must include non-empty alt attribute")


def validate_css_scoping(css: str) -> list[str]:
    errors: list[str] = []
    # Strip block comments.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    # Remove strings to avoid false positives.
    css = re.sub(r"'([^'\\]|\\.)*'", "''", css)
    css = re.sub(r'"([^"\\]|\\.)*"', '""', css)

    # Collect selector blocks. This is not a full CSS parser, but good enough for the repo rules.
    for block in css.split("{")[:-1]:
        selector_part = block.split("}")[-1].strip()
        if not selector_part:
            continue
        # Skip @ rules (handled by nested selectors later in the file).
        if selector_part.lstrip().startswith("@"):
            continue

        selectors = [s.strip() for s in selector_part.split(",") if s.strip()]
        for sel in selectors:
            if not sel.startswith(".chaos-region"):
                errors.append(f"All selectors must start with .chaos-region: {sel}")
                continue

            # Disallow bare element selectors anywhere in the selector chain.
            # Allow element names only when preceded by . or # or : or [ (i.e. part of a class/id/pseudo/attr selector).
            tokens = re.split(r"\s+|>\s*|\+\s*|~\s*", sel)
            for t in tokens:
                t = t.strip()
                if not t:
                    continue
                # Remove leading .chaos-region
                # Check for '*' or 'html'/'body' etc.
                if t in {"*", "html", "body"}:
                    errors.append(f"Bare element selector not allowed in chaos.css: {sel}")
                    break

                # Find any segment that begins with a letter (likely an element selector).
                # Examples to flag: 'h1', 'div.card', 'a:hover' (element selector)
                m = re.match(r"^[a-zA-Z][a-zA-Z0-9-]*", t)
                if m and not t.startswith((".", "#", ":", "[")):
                    # Allow .chaos-region itself.
                    if m.group(0) != "chaos-region":
                        errors.append(f"Bare element selector not allowed in chaos.css: {sel}")
                        break

    return errors


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


def validate(base_ref: str) -> ValidationResult:
    errs: list[str] = []
    files = changed_files(base_ref)

    illegal = [f for f in files if f not in ALLOWED_CHANGED_FILES]
    if illegal:
        errs.append("Only these files may change: " + ", ".join(sorted(ALLOWED_CHANGED_FILES)))
        errs.append("Illegal changed files: " + ", ".join(illegal))

    # Validate HTML boundaries and immutability outside chaos region.
    if "chaos-board/index.html" in files:
        base_html = read_file_at_ref(f"origin/{base_ref}", "chaos-board/index.html")
        head_html = read_worktree("chaos-board/index.html")
        try:
            base_before, base_region, base_after = split_chaos_region(base_html)
            head_before, head_region, head_after = split_chaos_region(head_html)
        except ValueError as e:
            errs.append(str(e))
        else:
            if base_before != head_before or base_after != head_after:
                errs.append("index.html may only change content between CHAOS_START/CHAOS_END markers")

            parser = ChaosHTMLSafetyParser()
            parser.feed(head_region)
            errs.extend(parser.errors)

    # Validate CSS scoping.
    if "chaos-board/assets/chaos.css" in files:
        css = read_worktree("chaos-board/assets/chaos.css")
        errs.extend(validate_css_scoping(css))

    return ValidationResult(ok=(len(errs) == 0), errors=errs)


def main() -> int:
    base_ref = get_base_ref()
    result = validate(base_ref)
    if result.ok:
        print("Chaos PR validation: OK")
        return 0

    print("Chaos PR validation: FAILED")
    for e in result.errors:
        print(f"- {e}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
