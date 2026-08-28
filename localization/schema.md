# Lokalisierungs-Schema

Source of Truth für alle UI-Strings: `localization/strings/<lang>.json`
(aktuell `de.json`, `en.json`). `tools/i18n/export.py` generiert daraus die
nativen Formate je Plattform. Diese Dateien nie direkt in den
Plattform-Verzeichnissen editieren — Änderungen gehen verloren, sobald das
Skript erneut läuft.

## Key-Konvention
Hierarchisch nach Feature/Screen, als verschachteltes JSON-Objekt. Jedes
Blatt ist entweder ein String oder ein Pluralform-Objekt.

```json
{
  "reader": {
    "markAsRead": "Als gelesen markieren"
  },
  "settings": {
    "tts": {
      "voice": "Stimme"
    }
  }
}
```

Voller Key zur Referenz: `reader.markAsRead`, `settings.tts.voice`.
Segmente camelCase, keine Sonderzeichen, keine Zahlen am Segment-Anfang.

## Pluralformen
Statt eines Strings ein Objekt mit den CLDR-Kategorien `one`/`other`
(für DE/EN ausreichend; weitere Kategorien bei Bedarf für künftige
Sprachen):

```json
{
  "reader": {
    "unreadCount": {
      "one": "{count} ungelesener Artikel",
      "other": "{count} ungelesene Artikel"
    }
  }
}
```

## Platzhalter
Benannte Platzhalter in geschweiften Klammern, z.B. `{count}`, `{title}`.
Das Export-Skript übersetzt sie pro Plattform in das jeweils native Format
(z.B. `%lld`/`%@` für iOS String Catalog, `%1$s` für Android, `{count}` bleibt
bei WebExtension-`messages.json` als `$COUNT$`-Platzhalter erhalten).

## Key-Parität
`de.json` und `en.json` müssen exakt dieselbe Schlüsselstruktur haben.
`export.py --check` bricht ab, wenn Keys fehlen oder zusätzlich vorhanden
sind.

## Sonderfall Android

`merlin-android` ist wie iOS ein nativer Client der generischen Namespaces
(`common.*`, `onboarding.*`, `articleReader.*`, ...) - `webext.*`/
`nextcloudWeb.*`/`merlinServer.*` gehören nicht dorthin und werden beim
Export herausgefiltert. `export.py --platform android` erzeugt daraus je
Sprache eine `res/values(-de)/strings_i18n.xml` (Default-Ordner `values`
für die Quellsprache `en`) mit `<string>`- und `<plurals>`-Ressourcen -
eigene generierte Datei statt Einträge in der bestehenden `strings.xml`,
damit von Hand gepflegte Ressourcen dort (aktuell nur `app_name`)
unangetastet bleiben; der Key `app.name` wird deshalb beim Android-Export
ausgeklammert. Platzhalter werden positionell (`%1$s`, `%2$d`, ...) statt
einfach (`%@`/`%lld` wie bei iOS) ausgegeben, da Android das bei mehr als
einem Platzhalter pro String zwingend verlangt.

## Sonderfall WebExtension (Thunderbird/Chrome/Firefox)
WebExtensions nutzen `browser.i18n` mit `_locales/<lang>/messages.json`.
Alle dafür bestimmten Strings liegen im Namespace `webext.*` (z.B.
`webext.options.urlLabel`, `webext.contextMenu.saveLink`). Das Export-Skript
(`export.py --platform webext`) erzeugt daraus pro Sprache eine
`messages.json` und bildet dabei ab:

- **Message-Namen**: `browser.i18n` erlaubt nur `[A-Za-z0-9_@]`. Der
  Dot-Key ohne `webext.`-Präfix wird auf Unterstriche gemappt:
  `webext.options.urlLabel` → Message `options_urlLabel`.
- **Platzhalter**: `{name}` → `$name$` im Message-Text plus eine
  `placeholders`-Tabelle, die jeden Namen in Reihenfolge des ersten
  Auftretens auf `$1`, `$2`, … abbildet. Der Code übergibt die Argumente
  passend: `browser.i18n.getMessage('flyout_serverError', [String(code)])`.

Die `webext.*`-Keys werden **nicht** in die iOS-Bundles exportiert (und
umgekehrt landen die App-Keys nicht in der `messages.json`). In den
Erweiterungsseiten (HTML) werden statische Texte über `data-i18n` /
`data-i18n-placeholder` / `data-i18n-title` markiert und von `i18n.js` zur
Laufzeit gefüllt; `manifest.json` nutzt `__MSG_extName__` /
`__MSG_extDescription__` mit `"default_locale": "en"`.

## Sonderfall Nextcloud
Nextcloud nutzt klassisches gettext: der Code ruft `$l->t('Englischer
Literal-String')` (PHP) bzw. `t('merlin', 'Englischer Literal-String')`
(Vue-Frontend, `@nextcloud/l10n`) auf - der englische Literal-String selbst
ist der Übersetzungs-Key, nicht der Dot-Key aus dieser JSON. Damit das mit
dieser zentralen Quelle funktioniert, muss der `en`-Wert eines Keys exakt
dem Literal im Code entsprechen (Zeichen für Zeichen, inkl. Groß-/
Kleinschreibung, Ellipsen `…`, Satzzeichen). Das Export-Skript erzeugt aus
dem `de`-Wert `l10n/de.json` + `l10n/de.js` im Nextcloud-Format (Mapping:
EN-Literal → DE-Übersetzung; `l10n/de.js` ruft `OC.L10N.register()` auf und
wird clientseitig eingebunden, `l10n/de.json` liest die PHP-seitige
`IL10N`-Klasse direkt ein). Englisch selbst braucht keine l10n-Datei
(Standardverhalten von gettext: Quellsprache ohne Übersetzungsdatei).

### Frontend (Vue) vs. Backend (PHP)
Beide teilen sich denselben `l10n/<lang>.json`/`.js`-Mechanismus, aber aktuell
exportiert `export.py --platform nextcloud` nur die Strings unter dem
`nextcloudWeb.`-Namespace (die Vue-Komponenten in `merlin-nextcloud/src/`).
PHP-seitige `$l->t()`-Aufrufe (z. B. in `ArticleSearchProvider.php`) sind
noch nicht an diese zentrale Quelle angebunden - dafür bräuchte es einen
eigenen Namespace (z. B. `nextcloudBackend.`) plus Erweiterung von
`build_nextcloud_translations`, um beide Namespaces in dieselbe
`l10n/<lang>.json` zu mergen.

### Pluralformen
Da Nextcloud gettext-Pluralformen über einen kombinierten Key aus Singular
und Plural aufschlüsselt, wird aus einem Pluralform-Objekt (siehe oben) der
Key `"<en.one>_::_<en.other>"` gebildet; der Wert ist ein Array
`[<de.one>, <de.other>]` in Plural-Index-Reihenfolge. Die Pluralregel selbst
(`"nplurals=2; plural=(n != 1);"`) ist für DE/EN identisch und im
Export-Skript fest hinterlegt (`NEXTCLOUD_PLURAL_FORM`).

### nextcloudWeb-Namespace
Die Strings der Vue-Komponenten liegen unter `nextcloudWeb.<component>.<key>`
(z. B. `nextcloudWeb.settings.fontSize`). Dieser Namespace ist bewusst
eigenständig und referenziert nicht die iOS-Keys (`common.*`,
`articleActions.*`, …), selbst wenn der englische Text ähnlich ist -
Groß-/Kleinschreibung und Wortwahl zwischen iOS- und Web-UI weichen an
mehreren Stellen ab (z. B. "Copy link" vs. "Copy Link"), und eine
fälschliche Wiederverwendung würde beim gettext-Lookup einfach keine
Übersetzung finden. Dasselbe Muster wie beim eigenständigen `webext.*`-
Namespace.

## Sonderfall merlin-server
`merlin-server` hat - anders als Nextcloud - keine gettext-Infrastruktur und
keinen Framework-i18n-Dienst (kein Vue, kein `@nextcloud/l10n`). Die Strings
liegen unter dem eigenständigen `merlinServer.<template>.<key>`-Namespace
(z. B. `merlinServer.account.paywallHeading`) und referenzieren keine anderen
Namespaces, aus denselben Gründen wie bei `webext.*`/`nextcloudWeb.*`.

`export.py --platform merlin-server` erzeugt daraus
`merlin-server/src/I18n/lang/<lang>.php` - je eine flache PHP-Array-Datei
(`return ['<dot-key ohne merlinServer.-Präfix>' => '<Wert>', ...];`) pro
Sprache, **beide Sprachen inklusive Englisch** (anders als bei Nextcloud gibt
es keine gettext-Quellsprache ohne eigene Datei - das PHP-Array *ist* der
Katalog, nicht bloß eine Übersetzungsdatei). `Merlin\I18n\Translator` lädt die
Datei der aufgelösten Sprache und schlägt Keys direkt nach (kein
Literal-String-Lookup wie bei Nextcloud) - der Dot-Key im Code ist also
identisch mit dem Dot-Key in `strings/<lang>.json` (minus Präfix), nicht mit
dem englischen Text.

`Merlin\I18n\LocaleResolver` ermittelt die Sprache je Request in dieser
Reihenfolge: gespeicherte Präferenz des eingeloggten Nutzers
(`user_settings`, Key `language`) > PHP-Session (deckt ausgeloggte Seiten wie
Login/Registrierung/Public-Share ab) > `Accept-Language`-Header > Default
`de` (unauffälliger Default für bestehende, bisher Deutsch-only laufende
Installationen). Der explizite Sprachwechsel läuft über `GET
/lang/{code}?return=<Pfad>`, das die Präferenz schreibt und zur aufrufenden
Seite zurückspringt (Link im Footer jeder Seite, `partials/footer.php`).

Für Strings, die innerhalb von `<script>`-Blöcken gebraucht werden (dynamisch
per JS gebaute Tabellenzeilen, `confirm()`-Dialoge, Status-Labels), gibt jede
Template-Datei ein whitelistetes `I18N`-Objekt aus
(`json_encode($t->forJs(['key1', 'key2', ...]))`) - kein globaler Dump aller
Keys, nur die auf der jeweiligen Seite tatsächlich benutzten.
