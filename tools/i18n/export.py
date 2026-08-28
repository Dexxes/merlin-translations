#!/usr/bin/env python3
"""
Lokalisierungs-Export für Merlin.

Liest localization/strings/<lang>.json (Source of Truth) und generiert
daraus native Lokalisierungsformate je Plattform.

Aktuell implementiert:
  - iOS / iPadOS: klassische .strings/.stringsdict in <lang>.lproj-Ordnern.
  - android: res/values(-de)/strings_i18n.xml (<string>/<plurals>) für
    merlin-android. Wie iOS werden webext.*/nextcloudWeb.*/merlinServer.*
    herausgefiltert (siehe without_prefix in export_android) - Android ist
    wie iOS ein nativer Client, der dieselben generischen Namespaces
    (common, onboarding, articleReader, ...) nutzt. Eigene generierte Datei
    statt Einträge in der bestehenden strings.xml, damit die dort von Hand
    gepflegten Ressourcen (z.B. app_name) unangetastet bleiben.
  - webext (Thunderbird): _locales/<lang>/messages.json für browser.i18n.
    Nur die Keys unter dem `webext.`-Namespace werden hier exportiert; iOS
    überspringt diese umgekehrt (siehe without_prefix in export_ios).
  - nextcloud: l10n/<lang>.json + l10n/<lang>.js für die Vue-Frontend-Strings
    der merlin-nextcloud-App (@nextcloud/l10n translate()/translatePlural()).
    Nur die Keys unter dem `nextcloudWeb.`-Namespace werden hier exportiert -
    siehe "Sonderfall Nextcloud" in schema.md. Der englische Literal-String
    selbst ist der gettext-Key, daher wird für "en" keine l10n-Datei erzeugt
    (Standardverhalten: Quellsprache ohne Übersetzungsdatei).
  - merlin-server: src/I18n/lang/<lang>.php - flaches PHP-Array (Dot-Key
    ohne `merlinServer.`-Präfix -> String bzw. Plural-Array) für
    Merlin\\I18n\\Translator. Anders als bei Nextcloud gibt es keine
    gettext-Infrastruktur, daher ist der Dot-Key selbst der
    Laufzeit-Lookup-Key (wie bei webext) - beide Sprachen bekommen eine
    Datei, auch Englisch (siehe "Sonderfall merlin-server" in schema.md).

Warum .strings/.stringsdict statt .xcstrings (Xcode String Catalog):
SwiftPM kompiliert .xcstrings nur, wenn der Build über xcodebuild/Xcode
läuft - bei einem reinen `swift build` (z. B. via xtool, das ohne Xcode
auf Linux/Windows/macOS baut) wird die Catalog-Kompilierung NICHT
ausgeführt, und String(localized:) fällt zur Laufzeit auf die rohen Keys
zurück. Klassische .strings/.stringsdict-Dateien sind dagegen bereits im
finalen Laufzeitformat - SwiftPM kopiert sie unverändert in die
Resource-Bundles, ganz ohne Kompilierschritt. Siehe tasks/lessons.md.

Aufruf:
    python3 export.py                          # alle Plattformen (ios + android + webext + nextcloud + merlin-server)
    python3 export.py --platform webext        # nur die WebExtension-_locales
    python3 export.py --platform ios
    python3 export.py --platform android       # nur merlin-android/.../res/values(-de)/strings_i18n.xml
    python3 export.py --platform nextcloud     # nur merlin-nextcloud/l10n
    python3 export.py --platform merlin-server  # nur merlin-server/src/I18n/lang
    python3 export.py --check                   # nur Key-Paritätscheck, kein Schreiben

Warum diese Reihenfolge (Validierung vor dem Schreiben):
Ein fehlender Key in einer Sprache fällt sonst erst beim App-Build oder zur
Laufzeit auf - hier brechen wir stattdessen sofort mit einer klaren
Fehlermeldung ab.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
STRINGS_DIR = REPO_ROOT / "localization" / "strings"
SOURCE_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en", "de"]

# Platzhalter-Konvention (siehe localization/schema.md):
# {count}/{code} sind immer Integer -> %lld, alles andere -> %@.
IOS_PLACEHOLDER_FORMATS = {
    "count": "%lld",
    "code": "%lld",
}
IOS_DEFAULT_PLACEHOLDER_FORMAT = "%@"

# Name des Platzhalters, der eine Pluralform steuert (siehe schema.md) -
# in allen aktuellen Keys immer "count".
PLURAL_PLACEHOLDER_NAME = "count"

# Resources-Wurzelordner je iOS-Target. Die Share-Extension hat ihr eigenes
# App-Bundle -> braucht eine eigene Kopie der lproj-Ordner, sonst findet
# String(localized:) dort nichts (siehe Package.swift).
IOS_RESOURCE_DIRS = [
    REPO_ROOT / "merlin-ios" / "Sources" / "Merlin" / "Resources",
    REPO_ROOT / "merlin-ios" / "Sources" / "MerlinShare" / "Resources",
]

# Veraltete Artefakte aus der früheren .xcstrings-basierten Pipeline -
# werden beim Export entfernt, damit kein toter/widersprüchlicher Stand
# im Resources-Ordner liegen bleibt.
OBSOLETE_IOS_FILES = ["Localizable.xcstrings"]

# Ressourcen-Wurzelordner von merlin-android. Jede unterstützte Sprache
# bekommt einen eigenen values(-<lang>)-Ordner; "en" ist die Quellsprache
# und landet daher im Default-Ordner "values" (Android-Konvention).
ANDROID_RES_DIR = REPO_ROOT / "merlin-android" / "app" / "src" / "main" / "res"
ANDROID_VALUES_DIRS = {"en": "values", "de": "values-de"}

# Eigene generierte Datei statt Einträge in der bestehenden strings.xml -
# dort liegt bislang nur app_name (von Hand gepflegt), das beim Re-Export
# nicht überschrieben werden soll.
ANDROID_GENERATED_FILENAME = "strings_i18n.xml"

# Platzhalter-Konvention analog IOS_PLACEHOLDER_FORMATS (siehe schema.md):
# {count}/{code} sind immer Integer -> %d, alles andere -> %s. Android
# braucht zusätzlich einen 1-basierten Positions-Index (%1$s, %2$d, ...),
# da mehrere Platzhalter in einem String sonst nicht eindeutig sind.
ANDROID_INT_PLACEHOLDER_NAMES = {"count", "code"}

# "app.name" -> Ressourcenname "app_name" würde mit dem bereits von Hand
# gepflegten android:label-Eintrag in res/values/strings.xml kollidieren
# (doppelte Ressourcendefinition = Build-Fehler) - hier ausgeklammert statt
# die bestehende Datei zu berühren.
ANDROID_SKIP_KEYS = {"app.name"}

# Namespace-Präfix für reine WebExtension-Strings (Thunderbird/Chrome/Firefox).
# Diese Keys gehören NICHT in die iOS-Bundles - sie werden dort
# herausgefiltert (siehe drop_prefix) und nur vom webext-Exporter verarbeitet.
WEBEXT_PREFIX = "webext."

# Zielordner für die WebExtension-Lokalisierung (browser.i18n / chrome.i18n
# erwarten _locales/<lang>/messages.json). Alle drei Erweiterungen teilen sich
# dieselbe generierte messages.json (denselben webext.*-Namespace); jede nutzt
# davon nur ihre relevante Teilmenge, ungenutzte Messages sind unschädlich.
WEBEXT_LOCALES_DIRS = [
    REPO_ROOT / "merlin-thunderbird" / "_locales",
    REPO_ROOT / "merlin-chrome" / "_locales",
    REPO_ROOT / "merlin-firefox" / "_locales",
]


# Namespace-Präfix für die merlin-server-Strings (serverseitige PHP-Templates
# + Merlin\I18n\Translator). Keine gettext-Infrastruktur wie bei Nextcloud -
# der Dot-Key selbst ist der Laufzeit-Lookup-Key, siehe "Sonderfall
# merlin-server" in schema.md.
MERLIN_SERVER_PREFIX = "merlinServer."

# Zielordner für die generierten merlin-server-Sprachdateien.
MERLIN_SERVER_LANG_DIR = REPO_ROOT / "merlin-server" / "src" / "I18n" / "lang"


# Namespace-Präfix für die Nextcloud-Web-Frontend-Strings (merlin-nextcloud,
# Vue-Komponenten via @nextcloud/l10n translate()/translatePlural()). Anders
# als bei webext ist der Key hier irrelevant für die Laufzeit - @nextcloud/l10n
# schlägt Übersetzungen über den englischen Literal-String selbst nach (klassisches
# gettext-Prinzip, siehe "Sonderfall Nextcloud" in schema.md). Der Namespace dient
# nur der Organisation innerhalb der zentralen strings/<lang>.json.
NEXTCLOUD_PREFIX = "nextcloudWeb."

# Zielordner für die generierten Nextcloud-Übersetzungsdateien.
NEXTCLOUD_L10N_DIR = REPO_ROOT / "merlin-nextcloud" / "l10n"

# Nextcloud-App-ID (siehe appinfo/info.xml <id>) - erster Arg von
# OC.L10N.register() in der generierten .js-Datei.
NEXTCLOUD_APP_ID = "merlin"

# CLDR-Pluralregel für Deutsch und Englisch ist identisch (nplurals=2), daher
# fest verdrahtet statt konfigurierbar - reicht für die aktuell unterstützten
# Sprachen (siehe SUPPORTED_LANGUAGES).
NEXTCLOUD_PLURAL_FORM = "nplurals=2; plural=(n != 1);"


def load_strings(lang: str) -> dict[str, Any]:
    path = STRINGS_DIR / f"{lang}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Verschachteltes JSON in Dot-Keys flachklopfen.

    Pluralform-Objekte (Keys "one"/"other" auf Blattebene) werden NICHT
    weiter aufgelöst, sondern als Wert (dict) durchgereicht - der Aufrufer
    erkennt sie daran, dass der Wert ein dict mit ausschließlich
    Plural-Kategorie-Keys ist.
    """
    out: dict[str, Any] = {}
    plural_categories = {"zero", "one", "two", "few", "many", "other"}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and set(value.keys()) <= plural_categories and value:
            out[full_key] = value
        elif isinstance(value, dict):
            out.update(flatten(value, full_key))
        else:
            out[full_key] = value
    return out


def without_prefix(flat: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Alle Keys herausfiltern, die mit `prefix` beginnen."""
    return {k: v for k, v in flat.items() if not k.startswith(prefix)}


def only_prefix(flat: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Nur die Keys behalten, die mit `prefix` beginnen, und das Präfix entfernen."""
    return {k[len(prefix):]: v for k, v in flat.items() if k.startswith(prefix)}


def check_key_parity(flat_by_lang: dict[str, dict[str, Any]]) -> None:
    key_sets = {lang: set(flat.keys()) for lang, flat in flat_by_lang.items()}
    base_lang = SOURCE_LANGUAGE
    base_keys = key_sets[base_lang]
    problems = []
    for lang, keys in key_sets.items():
        if lang == base_lang:
            continue
        missing = base_keys - keys
        extra = keys - base_keys
        if missing:
            problems.append(f"  [{lang}] fehlende Keys: {sorted(missing)}")
        if extra:
            problems.append(f"  [{lang}] zusätzliche Keys (nicht in {base_lang}): {sorted(extra)}")
    if problems:
        print("Key-Paritätscheck fehlgeschlagen:", file=sys.stderr)
        for p in problems:
            print(p, file=sys.stderr)
        sys.exit(1)


def ios_placeholder_format(placeholder_name: str) -> str:
    return IOS_PLACEHOLDER_FORMATS.get(placeholder_name, IOS_DEFAULT_PLACEHOLDER_FORMAT)


def ios_convert_placeholders(value: str) -> str:
    """{name} -> %@ bzw. %lld, je nach Konvention in schema.md."""

    def repl(match: "re.Match[str]") -> str:
        return ios_placeholder_format(match.group(1))

    return re.sub(r"\{(\w+)\}", repl, value)


def strings_escape(value: str) -> str:
    """Escaping für klassische .strings-Dateien (NeXT-Plist-Stringformat).

    Reihenfolge wichtig: Backslash zuerst, sonst werden frisch eingefügte
    Escape-Backslashes (z. B. von \\n) versehentlich nochmal escaped.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def build_strings_file(flat: dict[str, Any]) -> str:
    """Baut den Inhalt einer Localizable.strings-Datei (nur Nicht-Plural-Keys)."""
    lines = []
    for key in sorted(flat.keys()):
        value = flat[key]
        if isinstance(value, dict):
            continue  # Pluralform -> gehört ins .stringsdict, nicht hierher
        converted = ios_convert_placeholders(value)
        lines.append(f'"{strings_escape(key)}" = "{strings_escape(converted)}";')
    return "\n".join(lines) + "\n"


def plist_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_stringsdict_file(flat: dict[str, Any]) -> str | None:
    """Baut den Inhalt einer Localizable.stringsdict-Datei (nur Plural-Keys).

    Gibt None zurück, wenn es keine Pluralform-Keys gibt (dann wird keine
    Datei geschrieben).
    """
    plural_keys = {k: v for k, v in flat.items() if isinstance(v, dict)}
    if not plural_keys:
        return None

    entries = []
    for key in sorted(plural_keys.keys()):
        categories = plural_keys[key]
        format_key = f"%#@{PLURAL_PLACEHOLDER_NAME}@"
        category_entries = []
        for category, text in categories.items():
            converted = ios_convert_placeholders(text)
            category_entries.append(
                f"""            <key>{plist_escape(category)}</key>
            <string>{plist_escape(converted)}</string>"""
            )
        entries.append(
            f"""    <key>{plist_escape(key)}</key>
    <dict>
        <key>NSStringLocalizedFormatKey</key>
        <string>{plist_escape(format_key)}</string>
        <key>{plist_escape(PLURAL_PLACEHOLDER_NAME)}</key>
        <dict>
            <key>NSStringFormatSpecTypeKey</key>
            <string>NSStringPluralRuleType</string>
            <key>NSStringFormatValueTypeKey</key>
            <string>lld</string>
{chr(10).join(category_entries)}
        </dict>
    </dict>"""
        )

    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
{body}
</dict>
</plist>
"""


def export_ios(flat_by_lang: dict[str, dict[str, Any]], dry_run: bool) -> None:
    # Reine WebExtension- und Nextcloud-Web-Strings gehören nicht ins iOS-Bundle -
    # vor dem Generieren herausfiltern, damit die .strings-Dateien sauber
    # bleiben (sonst lägen ungenutzte webext-/nextcloudWeb-Keys in den
    # iOS-Resources).
    flat_by_lang = {
        lang: without_prefix(without_prefix(flat, WEBEXT_PREFIX), NEXTCLOUD_PREFIX)
        for lang, flat in flat_by_lang.items()
    }
    for resource_dir in IOS_RESOURCE_DIRS:
        for lang in SUPPORTED_LANGUAGES:
            lproj = resource_dir / f"{lang}.lproj"
            strings_text = build_strings_file(flat_by_lang[lang])
            stringsdict_text = build_stringsdict_file(flat_by_lang[lang])

            strings_path = lproj / "Localizable.strings"
            stringsdict_path = lproj / "Localizable.stringsdict"

            if dry_run:
                print(f"[dry-run] würde schreiben: {strings_path}")
                if stringsdict_text is not None:
                    print(f"[dry-run] würde schreiben: {stringsdict_path}")
                continue

            lproj.mkdir(parents=True, exist_ok=True)
            strings_path.write_text(strings_text, encoding="utf-8")
            print(f"geschrieben: {strings_path}")
            if stringsdict_text is not None:
                stringsdict_path.write_text(stringsdict_text, encoding="utf-8")
                print(f"geschrieben: {stringsdict_path}")

        # Alte .xcstrings-Artefakte aus der früheren Pipeline aufräumen,
        # damit kein widersprüchlicher/toter Stand im Resources-Ordner liegt.
        for obsolete_name in OBSOLETE_IOS_FILES:
            obsolete_path = resource_dir / obsolete_name
            if obsolete_path.exists():
                if dry_run:
                    print(f"[dry-run] würde löschen: {obsolete_path}")
                else:
                    obsolete_path.unlink()
                    print(f"gelöscht (veraltet): {obsolete_path}")


# ─── Android (merlin-android, res/values(-de)/strings_i18n.xml) ───────────────
#
# Wie iOS ein nativer Client der generischen Namespaces (common, onboarding,
# articleReader, ...) - webext.*/nextcloudWeb.*/merlinServer.* gehören nicht
# ins App-Bundle und werden herausgefiltert (ungenutzte Strings würden sonst
# unnötig die APK vergrößern).


def android_resource_name(dot_key: str) -> str:
    """Dot-Key in einen gültigen Android-Ressourcennamen wandeln (Punkt -> _)."""
    return dot_key.replace(".", "_")


def android_convert_placeholders(value: str) -> str:
    """`{name}` -> positionelles `%1$s`/`%1$d` (Reihenfolge des ersten Auftretens).

    Android verlangt bei mehreren Platzhaltern in einem String zwingend eine
    Positionsangabe (sonst schlägt aapt beim Build fehl), anders als iOS'
    einfaches %@/%lld.
    """
    order: list[str] = []

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in order:
            order.append(name)
        index = order.index(name) + 1
        fmt = "d" if name in ANDROID_INT_PLACEHOLDER_NAMES else "s"
        return f"%{index}${fmt}"

    return re.sub(r"\{(\w+)\}", repl, value)


def android_escape(value: str) -> str:
    """Escaping für strings.xml, Wert in Anführungszeichen gesetzt.

    Reihenfolge wichtig: Backslash zuerst (sonst werden frisch eingefügte
    Escapes doppelt escaped), XML-Entities vor dem Anführungszeichen-Escaping.
    In Anführungszeichen gesetzte Werte lässt aapt wortwörtlich stehen (die
    Quotes selbst werden entfernt) - erspart das sonst nötige Escaping von
    Apostrophen, das in unformatierten strings.xml-Werten sonst einen Build-
    Fehler auslöst.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def build_android_xml(flat: dict[str, Any]) -> str:
    """Baut den Inhalt einer strings_i18n.xml (<string>/<plurals>)."""
    bare = without_prefix(
        without_prefix(without_prefix(flat, WEBEXT_PREFIX), NEXTCLOUD_PREFIX),
        MERLIN_SERVER_PREFIX,
    )
    bare = {k: v for k, v in bare.items() if k not in ANDROID_SKIP_KEYS}
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!-- Automatisch generiert von tools/i18n/export.py --platform android. -->",
        "<!-- Nicht direkt editieren - Änderungen gehen beim nächsten Export verloren. -->",
        "<resources>",
    ]
    for dot_key in sorted(bare.keys()):
        value = bare[dot_key]
        name = android_resource_name(dot_key)
        if isinstance(value, dict):
            lines.append(f'    <plurals name="{name}">')
            for category in ("one", "other"):
                if category not in value:
                    continue
                converted = android_convert_placeholders(value[category])
                lines.append(f'        <item quantity="{category}">{android_escape(converted)}</item>')
            lines.append("    </plurals>")
        else:
            converted = android_convert_placeholders(str(value))
            lines.append(f'    <string name="{name}">{android_escape(converted)}</string>')
    lines.append("</resources>")
    return "\n".join(lines) + "\n"


def export_android(flat_by_lang: dict[str, dict[str, Any]], dry_run: bool) -> None:
    for lang in SUPPORTED_LANGUAGES:
        xml_text = build_android_xml(flat_by_lang[lang])
        values_dir = ANDROID_RES_DIR / ANDROID_VALUES_DIRS[lang]
        xml_path = values_dir / ANDROID_GENERATED_FILENAME

        if dry_run:
            print(f"[dry-run] würde schreiben: {xml_path}")
            continue

        values_dir.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml_text, encoding="utf-8")
        print(f"geschrieben: {xml_path}")


# ─── WebExtension (browser.i18n / _locales/<lang>/messages.json) ──────────────
#
# browser.i18n nutzt benannte Platzhalter, die über eine "placeholders"-Tabelle
# auf die Substitutions-Argumente ($1, $2, …) abgebildet werden. Aus unserem
# `{name}`-Format wird `$name$` im Message-Text plus ein placeholders-Eintrag
# { "name": { "content": "$1" } }. Die Reihenfolge der $1/$2-Indizes richtet
# sich nach dem ersten Auftreten im String - genauso muss der Code die Argumente
# an browser.i18n.getMessage(key, [...]) übergeben.


def webext_message_name(bare_key: str) -> str:
    """Dot-Key (ohne webext.-Präfix) in einen gültigen i18n-Message-Namen wandeln.

    browser.i18n erlaubt nur [A-Za-z0-9_@]; Punkte werden zu Unterstrichen.
    """
    return bare_key.replace(".", "_")


def webext_convert_placeholders(value: str) -> tuple[str, dict[str, dict[str, str]]]:
    """`{name}` -> `$name$` plus passende placeholders-Tabelle für messages.json."""
    order: list[str] = []

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in order:
            order.append(name)
        return f"${name}$"

    # Literale `$` müssen in messages.json als `$$` escaped werden (vor der
    # Platzhalter-Ersetzung, damit unsere frisch eingefügten `$name$` intakt
    # bleiben).
    escaped = value.replace("$", "$$")
    content = re.sub(r"\{(\w+)\}", repl, escaped)

    placeholders: dict[str, dict[str, str]] = {}
    for index, name in enumerate(order, start=1):
        placeholders[name] = {"content": f"${index}"}
    return content, placeholders


def build_messages_json(flat: dict[str, Any]) -> str:
    """Baut den Inhalt einer _locales/<lang>/messages.json (nur webext.*-Keys)."""
    bare = only_prefix(flat, WEBEXT_PREFIX)
    messages: dict[str, dict[str, Any]] = {}
    for bare_key in sorted(bare.keys()):
        value = bare[bare_key]
        # WebExtension-i18n kennt keine nativen Pluralformen - sollte je ein
        # Plural-Objekt unter webext.* landen, nehmen wir die "other"-Form.
        if isinstance(value, dict):
            value = value.get("other") or next(iter(value.values()))
        name = webext_message_name(bare_key)
        content, placeholders = webext_convert_placeholders(value)
        entry: dict[str, Any] = {"message": content}
        if placeholders:
            entry["placeholders"] = placeholders
        messages[name] = entry
    return json.dumps(messages, ensure_ascii=False, indent=2) + "\n"


def export_webext(flat_by_lang: dict[str, dict[str, Any]], dry_run: bool) -> None:
    for locales_dir in WEBEXT_LOCALES_DIRS:
        for lang in SUPPORTED_LANGUAGES:
            messages_text = build_messages_json(flat_by_lang[lang])
            messages_path = locales_dir / lang / "messages.json"

            if dry_run:
                print(f"[dry-run] würde schreiben: {messages_path}")
                continue

            messages_path.parent.mkdir(parents=True, exist_ok=True)
            messages_path.write_text(messages_text, encoding="utf-8")
            print(f"geschrieben: {messages_path}")



# ─── Nextcloud (merlin-nextcloud Vue-Frontend, @nextcloud/l10n) ───────────────
#
# @nextcloud/l10n's translate()/translatePlural() schlagen Übersetzungen über
# den englischen Literal-String selbst nach (klassisches gettext-Prinzip) -
# der Dot-Key aus strings/<lang>.json ist dafür irrelevant, siehe schema.md.
# Für Pluralformen erwartet Nextcloud als Lookup-Key
# "<singular>_::_<plural>" und als Wert ein Array der Übersetzungen in
# Plural-Index-Reihenfolge (siehe translationtool.phar-Ausgabe anderer
# Nextcloud-Apps).


def build_nextcloud_translations(en_flat: dict[str, Any], target_flat: dict[str, Any]) -> dict[str, Any]:
    """Baut die {Literal-EN: Übersetzung}-Map für l10n/<lang>.json.

    Iteriert über die EN-Werte (nicht die Dot-Keys!), weil der englische
    Literal-String der eigentliche gettext-Key ist. en_flat und target_flat
    müssen dieselben Dot-Keys haben (Key-Paritätscheck läuft vorher).
    """
    translations: dict[str, Any] = {}
    for dot_key, en_value in en_flat.items():
        if not dot_key.startswith(NEXTCLOUD_PREFIX):
            continue
        target_value = target_flat[dot_key]
        if isinstance(en_value, dict):
            # Pluralform: Key ist "singular_::_plural", Wert ein Array in
            # Plural-Index-Reihenfolge (Index 0 = "one", Index 1 = "other" -
            # reicht für die aktuelle DE/EN-Pluralregel, siehe NEXTCLOUD_PLURAL_FORM).
            msgid = f"{en_value['one']}_::_{en_value['other']}"
            translations[msgid] = [target_value['one'], target_value['other']]
        else:
            translations[en_value] = target_value
    return translations


def build_nextcloud_json(translations: dict[str, Any]) -> str:
    payload = {
        "translations": dict(sorted(translations.items())),
        "pluralForm": NEXTCLOUD_PLURAL_FORM,
    }
    return json.dumps(payload, ensure_ascii=False, indent=4) + "\n"


def build_nextcloud_js(translations: dict[str, Any]) -> str:
    # Format entspricht dem, was translationtool.phar für andere Nextcloud-Apps
    # erzeugt - OC.L10N.register lädt es clientseitig ohne zusätzlichen
    # Netzwerk-Request (die .json-Datei dient dem PHP-seitigen IL10N-Lookup).
    body_lines = []
    for key in sorted(translations.keys()):
        value = translations[key]
        if isinstance(value, list):
            value_js = "[" + ",".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
        else:
            value_js = json.dumps(value, ensure_ascii=False)
        body_lines.append(f'    {json.dumps(key, ensure_ascii=False)} : {value_js}')
    body = ",\n".join(body_lines)
    return (
        "OC.L10N.register(\n"
        f'    "{NEXTCLOUD_APP_ID}",\n'
        "    {\n"
        f"{body}\n"
        "},\n"
        f'"{NEXTCLOUD_PLURAL_FORM}");\n'
    )


def export_nextcloud(flat_by_lang: dict[str, dict[str, Any]], dry_run: bool) -> None:
    en_flat = flat_by_lang[SOURCE_LANGUAGE]
    for lang in SUPPORTED_LANGUAGES:
        # Englisch (Quellsprache) braucht keine l10n-Datei - gettext-Standardverhalten.
        if lang == SOURCE_LANGUAGE:
            continue
        translations = build_nextcloud_translations(en_flat, flat_by_lang[lang])
        json_text = build_nextcloud_json(translations)
        js_text = build_nextcloud_js(translations)

        json_path = NEXTCLOUD_L10N_DIR / f"{lang}.json"
        js_path = NEXTCLOUD_L10N_DIR / f"{lang}.js"

        if dry_run:
            print(f"[dry-run] würde schreiben: {json_path}")
            print(f"[dry-run] würde schreiben: {js_path}")
            continue

        NEXTCLOUD_L10N_DIR.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json_text, encoding="utf-8")
        print(f"geschrieben: {json_path}")
        js_path.write_text(js_text, encoding="utf-8")
        print(f"geschrieben: {js_path}")


# ─── merlin-server (Merlin\I18n\Translator, PHP-Array-Sprachdateien) ───────────────
#
# Keine gettext-Infrastruktur wie bei Nextcloud - der Dot-Key (ohne
# merlinServer.-Präfix) ist selbst der Laufzeit-Lookup-Key, den Translator::t()
# aufruft. Deshalb braucht - anders als bei Nextcloud - auch Englisch eine
# eigene Datei (das PHP-Array *ist* der Katalog, keine gettext-Fallback-Sprache).


def php_string_escape(value: str) -> str:
    """Escaping für PHP-Single-Quote-Strings ('...')."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_merlin_server_php(flat: dict[str, Any]) -> str:
    """Baut den Inhalt einer lang/<lang>.php-Datei (nur merlinServer.*-Keys)."""
    bare = only_prefix(flat, MERLIN_SERVER_PREFIX)
    lines = ["<?php", "", "// Automatisch generiert von tools/i18n/export.py --platform merlin-server.", "// Nicht direkt editieren - Änderungen gehen beim nächsten Export verloren.", "", "return ["]
    for key in sorted(bare.keys()):
        value = bare[key]
        php_key = php_string_escape(key)
        if isinstance(value, dict):
            one = php_string_escape(value.get("one", ""))
            other = php_string_escape(value.get("other", ""))
            lines.append(f"    '{php_key}' => ['one' => '{one}', 'other' => '{other}'],")
        else:
            php_value = php_string_escape(str(value))
            lines.append(f"    '{php_key}' => '{php_value}',")
    lines.append("];")
    return "\n".join(lines) + "\n"


def export_merlin_server(flat_by_lang: dict[str, dict[str, Any]], dry_run: bool) -> None:
    for lang in SUPPORTED_LANGUAGES:
        php_text = build_merlin_server_php(flat_by_lang[lang])
        php_path = MERLIN_SERVER_LANG_DIR / f"{lang}.php"

        if dry_run:
            print(f"[dry-run] würde schreiben: {php_path}")
            continue

        MERLIN_SERVER_LANG_DIR.mkdir(parents=True, exist_ok=True)
        php_path.write_text(php_text, encoding="utf-8")
        print(f"geschrieben: {php_path}")


PLATFORM_EXPORTERS = {
    "ios": export_ios,
    "android": export_android,
    "webext": export_webext,
    "nextcloud": export_nextcloud,
    "merlin-server": export_merlin_server,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORM_EXPORTERS.keys()) + ["all"],
        default="all",
        help="Welche Plattform exportieren (Standard: alle implementierten).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Nur Key-Paritätscheck durchführen, nichts schreiben.",
    )
    args = parser.parse_args()

    raw_by_lang = {lang: load_strings(lang) for lang in SUPPORTED_LANGUAGES}
    flat_by_lang = {lang: flatten(data) for lang, data in raw_by_lang.items()}
    check_key_parity(flat_by_lang)
    print(f"Key-Paritätscheck OK ({len(flat_by_lang[SOURCE_LANGUAGE])} Keys).")

    if args.check:
        return

    platforms = list(PLATFORM_EXPORTERS.keys()) if args.platform == "all" else [args.platform]
    for platform in platforms:
        PLATFORM_EXPORTERS[platform](flat_by_lang, dry_run=False)


if __name__ == "__main__":
    main()
