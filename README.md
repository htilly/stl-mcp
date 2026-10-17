# stl-mcp

Självhostad MCP-server där Claude skapar och stegvis justerar 3D-modeller för utskrift (mm),
med ett webbgränssnitt för att se, rotera och bläddra bland modellerna.

- **MCP** (streamable HTTP) på `/mcp`
- **Bibliotek** på `/`: alla modeller med förhandsbild, sök, uppdateras live
- **3D-vy** på `/m/<id>`: rotera/zooma, visa/dölj delar, versionshistorik, redigera parametrar,
  ladda ner STL. Vyn uppdateras **direkt** när Claude ändrar modellen.
- **Kommentarer**: klicka på en punkt i modellen och skriv vad som ska ändras. Claude ser
  punkten och vilken del av receptet som ligger där.
- **Mallar**: skruvhål (M2–M5, försänkta/cylindriska, mutterficka), värmeinsatser, skruvtorn,
  snäpplås, gångjärn och text.

## Idén: modeller är recept, inte filer

Varje modell sparas som ett *recept* (JSON) med versionshistorik. STL-filerna byggs från receptet.
Därför kan Claude göra små, precisa ändringar (`{"wall": 3}`) i stället för att göra om allt,
och man kan alltid backa till en tidigare version.

Typiskt arbetsflöde, med 3D-vyn öppen i webbläsaren bredvid chatten:

1. *"Gör en pennhållare med tre hål för 12 mm pennor"* → Claude anropar `create_model` och
   ger dig länken till 3D-vyn.
2. Claude anropar `render_model` för att själv se resultatet och rätta uppenbara fel.
3. *"Fyra hål, och lite högre"* → `update_model(id, {"n": 4, "h": 50})` → vyn uppdateras och
   historiken visar v2.
4. Du kan också ändra parametrarna direkt i webben. Claude ser ändringen nästa gång den läser
   modellen med `get_model`.
5. Eller peka: klicka **💬 Kommentera**, klicka på hålet och skriv *"2 mm större"*. Säg sedan
   *"titta på kommentarerna"*. Claude får punkten, delen och `near_nodes`, alltså vilken nod i
   receptet som ligger där (t.ex. `/root/children/1`, ett `screw_hole`). Den ändrar modellen
   och stänger kommentaren med `update_model(..., resolves=["c1"])`, eller svarar med
   `reply_to_comment`.

## Köra
```
cp .env.example .env     # fyll i MCP_TOKEN och PUBLIC_URL
docker compose up -d --build
```
Utan Docker: `pip install -r requirements.txt && MCP_TOKEN=... python server.py`

Containern lyssnar bara på `127.0.0.1:8765`. Sätt `BIND=0.0.0.0` för att exponera porten direkt,
eller sätt `DOMAIN` och kör `docker compose --profile caddy up -d` för automatiskt HTTPS.

**Automatisk deploy** till egen server med en self-hosted GitHub-runner: se
[`deploy/README.md`](deploy/README.md).

Öppna `http://localhost:8765/` och logga in med token, eller gå till `/?token=<token>`, som
sätter en cookie.

## Koppla till Claude
- Claude Desktop/Code: `claude mcp add --transport http stl https://<din-url>/mcp --header "Authorization: Bearer <token>"`
- claude.ai: Settings → Connectors → Add custom connector → `https://<din-url>/mcp`
  (claude.ai kan inte skicka egen header. Lägg då servern bakom din reverse proxy/OAuth,
  eller kör via Claude Desktop/Code.)

## MCP-verktyg
| verktyg | |
|---|---|
| `create_model` | ny modell från en CSG-spec (former, booleska operationer, transformer, variabler) |
| `create_enclosure` | låda + lock med hörnskruvar, distanser, utskärningar och lockhål |
| `create_from_script` | godtycklig trimesh-Python (kräver `ALLOW_SCRIPT=1`, kör kod på servern!) |
| `update_model` | ändra variabler/parametrar eller valfritt värde via JSON-pekare → ny version |
| `get_model` | receptet, mått, historik och öppna kommentarer (med `near_nodes`) |
| `reply_to_comment` | svara på en kommentar, eventuellt markera den löst |
| `render_model` | PNG-bild (iso/fram/ovan/höger) så att Claude kan granska modellen visuellt |
| `list_models`, `revert_model`, `duplicate_model`, `delete_model` | |

### CSG-spec i korthet
```json
{
  "vars": {"d": 12, "wall": 2, "h": 40, "n": 3},
  "root": {"op": "difference", "children": [
    {"shape": "box", "size": ["=n*(d+wall)+wall", "=d+2*wall", "=h"], "radius": 3, "round": "vertical"},
    {"shape": "cylinder", "d": "=d", "h": "=h", "translate": ["=wall+d/2", "=wall+d/2", "=wall"],
     "array": {"count": ["=n", 1, 1], "step": ["=d+wall", 0, 0]}}
  ]}
}
```
- Former: `box` (med `radius`), `cylinder`, `cone`, `sphere`, `tube`, `torus`, `wedge`,
  `extrude` (polygon/cirkel/rektangel-profil, `twist`, `scale_top`) och `revolve`
- Mallar: `screw_hole`, `heat_insert`, `nut_trap` (skärande: ytan vid z=0, går nedåt),
  `boss`, `snap_hook`, `hinge` (print-in-place eller med separat axel) och `text`
  (DejaVu Sans, upphöjd eller ingraverad). Mått för M2–M5 finns i `templates.py`.
- Operationer: `union`, `difference`, `intersection`, `hull`, `minkowski`
- Alla noder: `translate`, `rotate` (grader), `scale`, `mirror`, `array`, `polar`, `hidden`
- Flera delar (en STL per del): `{"parts": {"botten": {...}, "lock": {...}}}`
- Strängar som börjar med `=` är uttryck: `"=w/2+wall"`, `"=max(a,b)"`, `"=cos(30)"`
  (säker utvärdering, ingen kod körs)

Fullständig referens finns i `csg.py` (`SPEC_HELP`), och Claude får den i verktygsbeskrivningen.

### Justera med `update_model`
```jsonc
{"changes": {"wall": 3}}                          // CSG: variabel; låda: parameter
{"changes": {"/root/children/1/d": 8}}            // valfritt värde via JSON-pekare
{"changes": {"/root/children/-": {"shape": ...}}} // lägg till en nod
{"remove": ["/root/children/2"]}                  // ta bort en nod
{"params": {...}}                                 // ersätt hela receptet
{"changes": {...}, "resolves": ["c1"]}            // ...och markera kommentar c1 som åtgärdad
```

Exempel med mallar, en platta med försänkta M3-hål och ingraverad text:
```json
{"vars": {"t": 4}, "root": {"op": "difference", "children": [
  {"shape": "box", "size": [60, 30, "=t"], "radius": 3, "round": "vertical"},
  {"shape": "screw_hole", "size": "M3", "head": "countersunk", "h": "=t",
   "translate": [8, 15, "=t"], "array": {"count": [2, 1, 1], "step": [44, 0, 0]}},
  {"shape": "text", "text": "OK", "size": 8, "h": 1, "halign": "center", "valign": "center",
   "translate": [30, 15, "=t-0.6"]}
]}}
```

## Lagring
`STL_DIR/models/<id>/model.json` (recept och historik) och `v<N>/<del>.stl`. De 50 senaste
versionerna behåller sina filer. Äldre versioner kan återskapas med `revert_model`.

## Säkerhet
Sätt alltid `MCP_TOKEN` och lägg servern bakom HTTPS. Webbgränssnittet kräver token (cookie)
om inte `UI_PUBLIC=1`, som gör det läsbart för alla medan ändringar fortfarande kräver token.
`FILES_PUBLIC=1` gör bara STL-länkarna klickbara utan token (modell-id:n går att gissa). Lämna
`ALLOW_SCRIPT` av om servern är nåbar utifrån. CSG-specen är säker att låta Claude skriva.

three.js (MIT) levereras lokalt från `web/vendor/three`, så gränssnittet behöver ingen CDN.
Typsnittet DejaVu Sans (fri licens, se `fonts/LICENSE`) används för text-mallen.
