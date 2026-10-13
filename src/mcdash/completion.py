"""RCON console completion from the vanilla server's command tree.

RCON has no completion request, so suggestions come from the report that the
vanilla data generator writes (``reports/commands.json``: the whole Brigadier
tree, every literal and argument with its parser) plus ``registries.json``
(item, block, entity ids ...) and the online players.

A one-shot Job (``REPORT_SCRIPT``) runs the generator in the world's own image
against the server jar already on the world volume, and writes the reports to
``$MCD_STORE_DIR/dashboard/commands/``:

* ``<version>.commands.json`` and ``<version>.registries.json``, per Minecraft
  version, generated once and shared by every world on that version;
* ``worlds/<world>.json``: ``{"pod": ..., "version": ...[, "error": ...]}`` —
  which version the world's current server pod runs (``LATEST`` resolves at
  start, so only the pod knows).

``CommandTree.suggest`` walks the tree over what has been typed and returns
candidates for the last (partial) token.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcdash.models import Suggestion

# Run by the commands Job in the world's itzg image (sh, java, unzip; no
# Python). /data is the world volume (read-only), /out the store's
# dashboard/commands directory. Always records the outcome for this pod in
# worlds/<world>.json so the dashboard does not start another Job for it.
REPORT_SCRIPT = r"""
set -u
data=${MCD_DATA:-/data}
out=${MCD_OUT:-/out}
ptr="$out/worlds/$MCD_WORLD.json"
mkdir -p "$out/worlds"
record() {  # version [error]
  printf '{"pod": "%s", "version": "%s", "error": "%s"}\n' \
    "$MCD_POD" "$1" "${2:-}" > "$ptr.tmp.$$" && mv -f "$ptr.tmp.$$" "$ptr"
}
envf="$data/.install-vanilla.env"
if [ ! -f "$envf" ]; then
  record "" "not a vanilla server"
  echo "no $envf: not a vanilla server"; exit 0
fi
val() { sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" "$envf" | head -n 1; }
version=$(val VERSION)
server=$(val SERVER)
case "$version" in
  ''|.*|*[!A-Za-z0-9._-]*) record "" "unrecognised version"; exit 1 ;;
esac
dest="$out/$version"
if [ ! -s "$dest.commands.json" ]; then
  work=$(mktemp -d) || exit 1
  cd "$work" || exit 1
  # Bundler jars (1.18+) carry the real server inside; older ones are flat.
  if [ -n "$(unzip -p "$server" META-INF/versions.list 2>/dev/null)" ]; then
    set -- -DbundlerMainClass=net.minecraft.data.Main -jar "$server"
  else
    set -- -cp "$server" net.minecraft.data.Main
  fi
  if ! java -Xmx512m -XX:+UseSerialGC "$@" --reports --output "$work/gen" \
      || [ ! -s "$work/gen/reports/commands.json" ]; then
    record "$version" "the data generator failed"; exit 1
  fi
  if [ -s "$work/gen/reports/registries.json" ]; then
    cp "$work/gen/reports/registries.json" "$dest.registries.json.tmp.$$" \
      && mv -f "$dest.registries.json.tmp.$$" "$dest.registries.json"
  fi
  cp "$work/gen/reports/commands.json" "$dest.commands.json.tmp.$$" \
    && mv -f "$dest.commands.json.tmp.$$" "$dest.commands.json" || exit 1
  rm -rf "$work"
fi
record "$version"
echo "commands for $version recorded for $MCD_POD"
"""

VERSION_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
MAX_ITEMS = 50

# Parsers whose value can contain spaces: completion stops inside them.
GREEDY = {"minecraft:message"}
# Arguments made of several space-separated tokens.
WIDTH = {
    "minecraft:block_pos": 3,
    "minecraft:vec3": 3,
    "minecraft:vec2": 2,
    "minecraft:rotation": 2,
    "minecraft:column_pos": 2,
}
_COORD = re.compile(r"[~^]?(-?\d*\.?\d*)")
_INT = re.compile(r"-?\d+")
_NUM = re.compile(r"-?(\d+\.?\d*|\.\d+)")
_TIME = re.compile(r"-?(\d+\.?\d*|\.\d+)[dst]?")
_SELECTOR = re.compile(r"@[aeprsn](\[.*\])?")
_PLAYER = re.compile(r"[A-Za-z0-9_]{3,16}")
_UUID = re.compile(r"[0-9a-fA-F-]{32,36}")

_FIXED: dict[str, tuple[str, ...]] = {
    "brigadier:bool": ("true", "false"),
    "minecraft:gamemode": ("survival", "creative", "adventure", "spectator"),
    "minecraft:dimension": ("overworld", "the_nether", "the_end"),
    "minecraft:entity_anchor": ("eyes", "feet"),
    "minecraft:template_mirror": ("none", "front_back", "left_right"),
    "minecraft:template_rotation": (
        "none",
        "clockwise_90",
        "counterclockwise_90",
        "180",
    ),
    "minecraft:heightmap": (
        "world_surface",
        "motion_blocking",
        "motion_blocking_no_leaves",
        "ocean_floor",
    ),
    "minecraft:team_color": (
        "black",
        "dark_blue",
        "dark_green",
        "dark_aqua",
        "dark_red",
        "dark_purple",
        "gold",
        "gray",
        "dark_gray",
        "blue",
        "green",
        "aqua",
        "red",
        "light_purple",
        "yellow",
        "white",
        "reset",
    ),
}
# Parsers whose values are a registry (when the report has no ``registry``
# property). Older names (pre-1.19.3) included.
_PARSER_REGISTRY = {
    "minecraft:item_stack": "minecraft:item",
    "minecraft:item_predicate": "minecraft:item",
    "minecraft:block_state": "minecraft:block",
    "minecraft:block_predicate": "minecraft:block",
    "minecraft:particle": "minecraft:particle_type",
    "minecraft:entity_summon": "minecraft:entity_type",
    "minecraft:mob_effect": "minecraft:mob_effect",
    "minecraft:item_enchantment": "minecraft:enchantment",
}
_PLAYER_PARSERS = {
    "minecraft:entity",
    "minecraft:game_profile",
    "minecraft:score_holder",
}
_ENTITY_SELECTORS = ("@a", "@e", "@n", "@p", "@r", "@s")
_PLAYER_SELECTORS = ("@a", "@p", "@r", "@s")


@dataclass
class Node:
    name: str
    kind: str  # root | literal | argument
    children: dict[str, Node] = field(default_factory=dict)
    executable: bool = False
    parser: str = ""
    props: dict[str, Any] = field(default_factory=dict)
    redirect: list[str] | None = None

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> Node:
        kids = raw.get("children") or {}
        redirect = raw.get("redirect")
        return cls(
            name=name,
            kind=str(raw.get("type", "literal")),
            children={k: cls.parse(k, v) for k, v in kids.items()},
            executable=bool(raw.get("executable")),
            parser=str(raw.get("parser", "")),
            props=dict(raw.get("properties") or {}),
            redirect=[str(p) for p in redirect] if isinstance(redirect, list) else None,
        )


def _balanced(tok: str) -> bool:
    depth = 0
    quote = ""
    for ch in tok:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
    return depth == 0 and not quote


def _short(value: str) -> str:
    return value.removeprefix("minecraft:")


class CommandTree:
    """A Brigadier command tree (``commands.json``) with registry values."""

    def __init__(
        self,
        commands: dict[str, Any],
        registries: dict[str, Any] | None = None,
    ) -> None:
        if commands.get("type") != "root" or not isinstance(
            commands.get("children"), dict
        ):
            raise ValueError("not a commands.json report")
        self.root = Node.parse("", commands)
        self.registries: dict[str, list[str]] = {}
        for key, reg in (registries or {}).items():
            entries = reg.get("entries") if isinstance(reg, dict) else None
            if isinstance(entries, dict):
                self.registries[key] = sorted(_short(e) for e in entries)

    @classmethod
    def load(cls, commands: Path, registries: Path | None = None) -> CommandTree:
        regs = None
        if registries is not None and registries.is_file():
            regs = json.loads(registries.read_text())
        return cls(json.loads(commands.read_text()), regs)

    # ------------------------------------------------------------ walking
    def _kids(self, node: Node) -> dict[str, Node]:
        if node.redirect is not None:
            target = self.root
            for part in node.redirect:
                target = target.children.get(part, target)
            return target.children
        if node.kind == "literal" and not node.children and not node.executable:
            # e.g. "execute ... run": a redirect to the root that the report
            # writes without a path.
            return self.root.children
        return node.children

    def _accepts(self, node: Node, tok: str) -> bool:
        p = node.parser
        if not tok or not _balanced(tok):
            return False
        if p in ("brigadier:integer", "brigadier:long"):
            return bool(_INT.fullmatch(tok))
        if p in ("brigadier:float", "brigadier:double"):
            return bool(_NUM.fullmatch(tok))
        if p == "minecraft:time":
            return bool(_TIME.fullmatch(tok))
        if p in WIDTH:
            return bool(_COORD.fullmatch(tok))
        if p in ("minecraft:entity", "minecraft:game_profile"):
            return bool(
                _SELECTOR.fullmatch(tok)
                or _PLAYER.fullmatch(tok)
                or _UUID.fullmatch(tok)
            )
        if p in _FIXED and p != "minecraft:dimension":
            return tok in _FIXED[p]
        return True

    def _walk(self, tokens: list[str]) -> tuple[list[Node], list[Node]]:
        """Nodes reached after ``tokens``, and arguments the next token is in.

        Follows Brigadier: a matching literal wins over the arguments beside
        it; otherwise every argument that accepts the token is followed.
        """
        ends: list[Node] = []
        inside: list[Node] = []
        stack: list[tuple[Node, int]] = [(self.root, 0)]
        seen: set[tuple[int, int]] = set()
        while stack:
            node, i = stack.pop()
            if (id(node), i) in seen:
                continue
            seen.add((id(node), i))
            if i == len(tokens):
                ends.append(node)
                continue
            kids = self._kids(node)
            tok = tokens[i]
            lit = kids.get(tok) or kids.get(tok.lower())
            if lit is not None and lit.kind == "literal":
                stack.append((lit, i + 1))
                continue
            for child in kids.values():
                if child.kind != "argument":
                    continue
                if child.parser in GREEDY or child.props.get("type") == "greedy":
                    inside.append(child)  # the rest of the line is one argument
                    continue
                width = WIDTH.get(child.parser, 1)
                part = tokens[i : i + width]
                if not all(self._accepts(child, t) for t in part):
                    continue
                if i + width <= len(tokens):
                    stack.append((child, i + width))
                else:
                    inside.append(child)
        return ends, inside

    # ------------------------------------------------------------ values
    def _values(self, node: Node, players: list[str]) -> list[str]:
        p = node.parser
        if p in _PLAYER_PARSERS:
            players_only = node.props.get("type") == "players"
            sel = _PLAYER_SELECTORS if players_only else _ENTITY_SELECTORS
            if p == "minecraft:game_profile":
                sel = ()
            return [*sorted(players, key=str.lower), *sel]
        if p in _FIXED:
            return list(_FIXED[p])
        reg = node.props.get("registry") or _PARSER_REGISTRY.get(p)
        if reg:
            return self.registries.get(str(reg), [])
        return []

    @staticmethod
    def _match(value: str, partial: str) -> int:
        """0: prefix match, 1: a ``_``/``.``/``/`` word matches, -1: no match."""
        if not partial:
            return 0
        v, p = value.lower(), _short(partial.lower())
        if v.startswith(p):
            return 0
        if any(w.startswith(p) for w in re.split(r"[_./]", v)[1:]):
            return 1
        return -1

    @staticmethod
    def _hint(node: Node) -> Suggestion:
        return Suggestion(
            text=f"<{node.name}>",
            kind="hint",
            detail=_short(node.parser.split(":")[-1]) if node.parser else "",
        )

    def suggest(
        self, line: str, players: list[str] | None = None
    ) -> tuple[int, list[Suggestion]]:
        """(start offset of the token being completed, candidates).

        Candidates of kind ``hint`` describe an argument (``<targets>``) and are
        not meant to be inserted.
        """
        body = line.lstrip().removeprefix("/")
        *done, partial = body.split(" ")
        start = len(line) - len(partial)
        ends, inside = self._walk(done)
        ranked: list[tuple[int, int, str, Suggestion]] = []
        hints: list[Suggestion] = [self._hint(n) for n in inside]
        for node in ends:
            for child in self._kids(node).values():
                if child.kind == "literal":
                    m = self._match(child.name, partial)
                    if m >= 0:
                        ranked.append((m, 0, child.name, Suggestion(child.name)))
                    continue
                values = self._values(child, players or [])
                for v in values:
                    m = self._match(v, partial)
                    if m >= 0:
                        ranked.append((m, 1, v.lower(), Suggestion(v, kind="value")))
                hints.append(self._hint(child))
        out: list[Suggestion] = []
        seen: set[str] = set()
        for *_, s in sorted(ranked, key=lambda r: r[:3]):
            if s.text not in seen and s.text != partial:
                seen.add(s.text)
                out.append(s)
        out = out[:MAX_ITEMS]
        for h in hints:
            if h.text not in seen:
                seen.add(h.text)
                out.append(h)
        return start, out
