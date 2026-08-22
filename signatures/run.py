#!/usr/bin/env python3
"""Lexically compare the complete public C++ declaration surface.

This deliberately does not preprocess the headers: canonical headers require
userver while Boost headers require Asio.  The token parser ignores function
bodies and private implementation, and records public types, aliases and
callable declarations without needing either dependency graph.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
OUTPUT = CONFORMANCE_DIR / ".artifacts" / "signatures" / "summary.json"
TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"[A-Za-z_]\w*|\d+(?:\.\d+)?|::|->\*|->|<=>|<<=|>>=|"
    r"&&|\|\||==|!=|<=|>=|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|"
    r"\.\.\.|\[\[|\]\]|[{}()\[\];,:<>~=+\-*/%&|^!.?]"
)
CONTROL = {"if", "for", "while", "switch", "catch", "return", "sizeof", "alignof"}
TYPE_WORDS = {"class", "struct", "union", "enum"}
PARAMETER_KEYWORDS = {
    "const", "volatile", "typename", "class", "struct", "enum", "auto",
    "void", "bool", "char", "signed", "unsigned", "short", "int", "long",
    "float", "double", "wchar_t", "char8_t", "char16_t", "char32_t",
}


def clean_source(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r"^[ \t]*#(?:[^\n\\]|\\\n)*\n", " ", text, flags=re.MULTILINE)
    return text


def tokens(path: Path) -> list[str]:
    return TOKEN.findall(clean_source(path.read_text(encoding="utf-8")))


def normalized(parts: list[str]) -> str:
    return " ".join(parts)


@dataclass
class Scope:
    name: str
    kind: str
    access: str = "public"
    visible: bool = True


class Extractor:
    def __init__(self, parts: list[str]):
        self.parts = parts
        self.i = 0
        self.declarations: list[str] = []
        self.errors: list[str] = []

    def extract(self) -> list[str]:
        self._scope([], None)
        if self.i != len(self.parts):
            self.errors.append(f"unconsumed tokens at {self.i}/{len(self.parts)}")
        return self.declarations

    def _qualified(self, scopes: list[Scope]) -> str:
        return "::".join(scope.name for scope in scopes if scope.name)

    def _record(self, category: str, scopes: list[Scope], statement: list[str]) -> None:
        owner = self._qualified(scopes)
        if category == "callable":
            statement = self._canonical_callable(statement)
        self.declarations.append(f"{category}|{owner}|{normalized(statement)}")

    def _canonical_callable(self, statement: list[str]) -> list[str]:
        # Constructor member initializers are implementation, never signature.
        round_depth = square_depth = angle_depth = 0
        truncated = list(statement)
        for index, part in enumerate(statement):
            if part == "(":
                round_depth += 1
            elif part == ")" and round_depth:
                round_depth -= 1
            elif part == "[":
                square_depth += 1
            elif part == "]" and square_depth:
                square_depth -= 1
            elif part == "<" and round_depth == square_depth == 0:
                angle_depth += 1
            elif part == ">" and angle_depth and round_depth == square_depth == 0:
                angle_depth -= 1
            elif part == ":" and round_depth == square_depth == angle_depth == 0:
                truncated = statement[:index]
                break

        pairs: list[tuple[int, int]] = []
        stack: list[int] = []
        angle_depth = square_depth = 0
        for index, part in enumerate(truncated):
            if part == "<" and not stack and square_depth == 0:
                angle_depth += 1
            elif part == ">" and angle_depth and not stack and square_depth == 0:
                angle_depth -= 1
            elif part == "[" and not stack:
                square_depth += 1
            elif part == "]" and square_depth and not stack:
                square_depth -= 1
            elif part == "(" and angle_depth == square_depth == 0:
                stack.append(index)
            elif part == ")" and stack:
                opening = stack.pop()
                if not stack:
                    pairs.append((opening, index))
        candidates = [
            pair for pair in pairs
            if pair[0] == 0 or truncated[pair[0] - 1] not in {"noexcept", "decltype", "requires"}
        ]
        if not candidates:
            return truncated
        opening, closing = candidates[-1]
        params = self._strip_parameter_names(truncated[opening + 1 : closing])
        return truncated[: opening + 1] + params + truncated[closing:]

    def _strip_parameter_names(self, parts: list[str]) -> list[str]:
        segments: list[list[str]] = []
        current: list[str] = []
        round_depth = square_depth = angle_depth = 0
        for part in parts:
            if part == "(": round_depth += 1
            elif part == ")" and round_depth: round_depth -= 1
            elif part == "[": square_depth += 1
            elif part == "]" and square_depth: square_depth -= 1
            elif part == "<": angle_depth += 1
            elif part == ">" and angle_depth: angle_depth -= 1
            if part == "," and round_depth == square_depth == angle_depth == 0:
                segments.append(current)
                current = []
            else:
                current.append(part)
        segments.append(current)

        result: list[str] = []
        for segment_index, segment in enumerate(segments):
            declaration = segment
            equals = next((idx for idx, part in enumerate(segment) if part == "="), len(segment))
            prefix = segment[:equals]
            identifiers = [
                idx for idx, part in enumerate(prefix)
                if re.fullmatch(r"[A-Za-z_]\w*", part)
                and part not in PARAMETER_KEYWORDS
                and not (idx and prefix[idx - 1] == "::")
                and not (idx + 1 < len(prefix) and prefix[idx + 1] == "::")
            ]
            if identifiers:
                candidate = identifiers[-1]
                has_type_before = len(identifiers) > 1 or candidate > 0
                if has_type_before and candidate == len(prefix) - 1:
                    declaration = prefix[:candidate] + segment[equals:]
            if segment_index:
                result.append(",")
            result.extend(declaration)
        return result

    def _public(self, scope: Scope | None) -> bool:
        return scope is None or (scope.visible and (scope.kind == "namespace" or scope.access == "public"))

    def _skip_balanced(self, opening: str = "{", closing: str = "}") -> None:
        depth = 1
        while self.i < len(self.parts) and depth:
            token = self.parts[self.i]
            self.i += 1
            if token == opening:
                depth += 1
            elif token == closing:
                depth -= 1
        if depth:
            self.errors.append(f"unterminated {opening}{closing} block")

    def _scope(self, scopes: list[Scope], current: Scope | None) -> None:
        pending: list[str] = []
        round_depth = square_depth = angle_depth = 0
        while self.i < len(self.parts):
            token = self.parts[self.i]

            if token == "}" and not pending and round_depth == square_depth == angle_depth == 0:
                self.i += 1
                return

            if (
                current is not None
                and current.kind in {"class", "struct", "union"}
                and not pending
                and token in {"public", "private", "protected"}
                and self.i + 1 < len(self.parts)
                and self.parts[self.i + 1] == ":"
            ):
                current.access = token
                self.i += 2
                continue

            pending.append(token)
            self.i += 1
            if token == "(":
                round_depth += 1
            elif token == ")":
                round_depth = max(0, round_depth - 1)
            elif token == "[":
                square_depth += 1
            elif token == "]":
                square_depth = max(0, square_depth - 1)
            elif token == "<" and round_depth == square_depth == 0:
                angle_depth += 1
            elif token == ">" and angle_depth and round_depth == square_depth == 0:
                angle_depth -= 1

            at_boundary = round_depth == square_depth == angle_depth == 0
            if token == ";" and at_boundary:
                self._semicolon(scopes, current, pending[:-1])
                pending = []
                continue
            if token != "{" or not at_boundary:
                continue

            header = pending[:-1]
            pending = []
            kind_index = next((idx for idx, part in enumerate(header) if part in TYPE_WORDS | {"namespace"}), None)
            kind = header[kind_index] if kind_index is not None else ""
            if kind in {"namespace", "class", "struct", "union"}:
                name_index = (kind_index or 0) + 1
                if kind == "enum" and name_index < len(header) and header[name_index] in {"class", "struct"}:
                    name_index += 1
                name = header[name_index] if name_index < len(header) else "<anonymous>"
                if kind != "namespace" and self._public(current):
                    self._record("type", scopes, header)
                nested = Scope(
                    name=name if name != "<anonymous>" else "",
                    kind="namespace" if kind == "namespace" else kind,
                    access="private" if kind == "class" else "public",
                    visible=self._public(current) and name != "detail",
                )
                self._scope(scopes + [nested], nested)
                if self.i < len(self.parts) and self.parts[self.i] == ";":
                    self.i += 1
                continue
            if kind == "enum":
                if self._public(current):
                    self._record("type", scopes, header)
                self._skip_balanced()
                if self.i < len(self.parts) and self.parts[self.i] == ";":
                    self.i += 1
                continue

            if self._looks_callable(header):
                if self._public(current):
                    self._record("callable", scopes, header)
                self._skip_balanced()
                continue

            # Lambda/aggregate initializer or another implementation block.
            self._skip_balanced()

        if current is not None:
            self.errors.append(f"unterminated scope {self._qualified(scopes)}")

    def _looks_callable(self, statement: list[str]) -> bool:
        if "(" not in statement or not statement:
            return False
        first = next((part for part in statement if part not in {"template", "<", ">", ","}), "")
        if first in CONTROL:
            return False
        angle = square = 0
        for index, part in enumerate(statement):
            if part == "<":
                angle += 1
            elif part == ">" and angle:
                angle -= 1
            elif part == "[":
                square += 1
            elif part == "]" and square:
                square -= 1
            elif part == "(" and angle == square == 0:
                if "=" in statement[:index] and "operator" not in statement[:index]:
                    return False
                return True
        return False

    def _semicolon(self, scopes: list[Scope], current: Scope | None, statement: list[str]) -> None:
        if not statement or not self._public(current):
            return
        if statement[:2] == ["using", "namespace"]:
            return
        if statement[0] in {"using", "typedef"} or "concept" in statement:
            self._record("alias", scopes, statement)
        elif self._looks_callable(statement):
            self._record("callable", scopes, statement)
        elif any(part in TYPE_WORDS for part in statement):
            self._record("type", scopes, statement)


def extract(path: Path) -> tuple[collections.Counter[str], list[str]]:
    extractor = Extractor(tokens(path))
    return collections.Counter(extractor.extract()), extractor.errors


def normalize_declarations(
    declarations: collections.Counter[str], substitutions: dict[str, str]
) -> collections.Counter[str]:
    result: collections.Counter[str] = collections.Counter()
    ordered = sorted(substitutions.items(), key=lambda item: len(item[0]), reverse=True)
    for declaration, count in declarations.items():
        normalized_declaration = declaration
        for source, replacement in ordered:
            normalized_declaration = normalized_declaration.replace(source, replacement)
        result[normalized_declaration] += count
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=HERE / "deviations.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    root = args.root.resolve()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    allowed = policy.get("signature_deviations", {})
    substitutions = policy.get("boundary_type_substitutions", {})
    canonical_root = root / "cppservicelib" / "include" / "servicelib"
    boost_root = root / "cppboostservicelib" / "include" / "servicelib"
    shared = sorted(
        path.relative_to(canonical_root).as_posix()
        for path in canonical_root.rglob("*.hpp")
        if (boost_root / path.relative_to(canonical_root)).is_file()
    )

    files: dict[str, object] = {}
    errors: list[str] = []
    total_canonical = total_boost = 0
    for relative in shared:
        canonical_raw, canonical_errors = extract(canonical_root / relative)
        boost_raw, boost_errors = extract(boost_root / relative)
        canonical = normalize_declarations(canonical_raw, substitutions)
        boost = normalize_declarations(boost_raw, substitutions)
        total_canonical += sum(canonical.values())
        total_boost += sum(boost.values())
        only_canonical = sorted((canonical - boost).elements())
        only_boost = sorted((boost - canonical).elements())
        different = bool(only_canonical or only_boost)
        permitted = allowed.get(relative)
        expected_canonical = sorted((permitted or {}).get("canonical_only", []))
        expected_boost = sorted((permitted or {}).get("boost_only", []))
        if canonical_errors or boost_errors:
            errors.append(f"parser:{relative}")
        if only_canonical != expected_canonical or only_boost != expected_boost:
            errors.append(f"deviation-mismatch:{relative}")
        if different or canonical_errors or boost_errors:
            files[relative] = {
                "canonical_only": only_canonical,
                "boost_only": only_boost,
                "canonical_parser_errors": canonical_errors,
                "boost_parser_errors": boost_errors,
                "deviation": permitted,
            }

    summary = {
        "status": "pass" if not errors else "fail",
        "shared_headers": len(shared),
        "canonical_declarations": total_canonical,
        "boost_declarations": total_boost,
        "different_headers": len(files),
        "boundary_type_substitutions": substitutions,
        "files": files,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
