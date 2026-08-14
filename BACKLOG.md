# Backlog

Lo pedido y aún no hecho. El estado de lo que sí está hecho vive en
`ootmm-autotracker-poc.md`.

## Lo siguiente (pedido el 13 ago 2026)

### A. (resuelto) La localización exacta del jugador

**Hecho el 14 ago 2026.** La escena sale del `PlayState`
(`OoT 0x801C84A0`, `MM 0x803E6B20`, `sceneId` en `+0xA4`), con barrido de
respaldo por la firma de `GameState` y caída al save context si no hay
partida. Se midió que el defecto era de los **dos** juegos: con el jugador
dentro de la tienda Kokiri, el save context decía Kokiri Forest.

De propina salió la **sala** (`roomCtx.curRoom.num`): los pendientes de la
sala en la que estás salen primero y marcados. Ver el POC.

### A2. (resuelto) Los setups de escena

**Hecho el 14 ago 2026**, del fallo de Hyrule Field: `Hyrule Field Bush 09`
(setup 1) y `Hyrule Field Grass Pack 3 Bush 09` (setup 0) son arbustos
distintos, de dos headers alternativos de la misma escena. El panel listaba
los de todos los setups, así que la mitad eran inalcanzables y se quedaban
pendientes para siempre. Ahora se lee `gSaveContext.sceneSetupId`
(`SaveContext+0x1360` OoT, `+0x3CAC` MM) y se resuelve como hace `oot/room.c`.
Ver el POC.

Cabo suelto: la sala sólo la llevan los xflags (4.440 de 6.043). Los cofres,
NPC y tiendas **nunca se filtran** por sala, así que en `GROTTOS` —una sola
escena con todas las grutas— quedan 23 de otras grutas en la lista aunque el
resto ya esté acotado (452 → 99). Para afinarlos haría falta su `roomId`, que
no está en ninguna de las fuentes actuales.

Y **no se saca de `COMBO_VROM_CHECKS`**, aunque el comentario de la clave
diga `(roomId << 8)`: para los cofres ese byte va a cero. Se sabe sin
comprobar nada nuevo — `override_key` forma la clave de los cofres con room 0
y casan las 367, cosa imposible si el byte llevara la sala. El id del cofre
es global dentro de su escena. Para tenerla habría que leer la lista de
actores por sala de los datos de escena de la ROM, que es otro trabajo.

### B. (resuelto) Un `.exe` para el usuario de a pie

**Hecho el 14 ago 2026.** `python -m PyInstaller ootmm.spec` →
`dist/ootmm-tracker.exe`, 8,5 MB, fichero único. Doble clic y arranca el
overlay. Detalle y medidas en el POC.

Las tres cosas que había que mirar, resueltas: `paths.py` separa lo que viaja
(`res()`, `sys._MEIPASS`) de lo que se genera (`user()`,
`%LOCALAPPDATA%\OoTMM-Tracker\`); los generadores ya no se lanzan por
subproceso —dentro del `.exe` `sys.executable` es el tracker, no un
intérprete—, se importan y se les llama a `main(argv)`; y lo de los antivirus
está avisado en el README.

De propina: `tracker.lua` viaja dentro y se instala solo en la carpeta del
emulador la primera vez (`install-lua` para hacerlo aparte), sin pisar nunca
un script que ya esté ahí.

Cabo suelto: sólo se ha construido en esta máquina, y el ejecutable no está
firmado.

### Firmar el `.exe`: cuándo y cuánto

**Preguntado el 14 ago 2026**, al pasárselo a un amigo y saltarle SmartScreen.
Decisión: **esperar a repartirlo de verdad**.

Por qué esperar, y no es sólo el dinero:

- El certificado es un coste **anual recurrente**. Empezar ahora quema meses
  mientras el único que lo usa es alguien de confianza, para quien el rodeo de
  SmartScreen son diez segundos.
- **Firmar no cambia la construcción**: se firma el `.exe` ya hecho con
  `signtool`, después de PyInstaller. No hay nada que reestructurar ahora para
  poder añadirlo luego.
- El único argumento en contra: con un OV la reputación de SmartScreen se
  acumula con las descargas, así que conviene llegar con el certificado algo
  rodado. Con un proyecto de hobby eso es despreciable — que es por lo que
  existe el EV.

**Órdenes de magnitud, NO presupuesto.** Los precios y sobre todo los
**requisitos de elegibilidad se mueven mucho**; hay que consultarlo el día que
toque.

| Opción | Orden | Lo que importa |
|---|---|---|
| **Azure Trusted Signing** | ~10 $/mes | Lo más barato con diferencia y sin token. **Mirarlo primero.** Los requisitos han cambiado varias veces (entidad legal con antigüedad al principio, luego se abrió a particulares): eso es lo que hay que verificar |
| **Certum Open Source** | decenas de €/año | La vía barata **si el proyecto es de código abierto**. Token incluido la primera vez |
| **OV** (Sectigo, DigiCert, SSL.com) | ~200–400 $/año | Desde 2023 la clave va en hardware: súmale token (~50–100 $) o firma en la nube. **No** da confianza inmediata |
| **EV** | ~300–600 $/año | El que da reputación de SmartScreen **desde el primer día**. Token obligatorio. Históricamente pedía entidad registrada |

**Esto lo decide la licencia**, que sigue sin elegir: con una licencia libre,
Certum es de largo lo más barato; cerrado, toca Azure/OV/EV.

Lo que **no** sirve: autofirmarlo. No mejora SmartScreen en nada.

Y cuando se haga: **sellar el tiempo** (`signtool sign /fd sha256 /tr <servidor>
/td sha256`), o las firmas dejan de valer al caducar el certificado.

## (hecho) Piezas de corazón y peces pequeños, a basura

**Pedido el 14 ago 2026.** Van a `JUNK_PATTERNS` / `is_junk()` en `overlay.py`,
así que entran por el filtro que ya existe y salen solos en el panel de
pendientes, en los contadores de «lo que importa» y en las barras de regiones.

- **`Piece of Heart`** sí. **`Heart Container` no**, que es un corazón entero.
  `Recovery Heart` ya era basura de antes.
- **Los peces, según lo que pesan**, que es la parte que no es obvia.

### El umbral de los peces sale de la lógica del generador

Un pez es relleno **o no según cuánto pese**: el estanque da premio por uno lo
bastante grande. `data/world/oot/overworld/lake_hylia.yml` lo dice:

```
"Fishing Pond Child": ... has_pond_fish(CHILD_FISH, 7, 14) || has_pond_fish(CHILD_LOACH, 14, 19)
"Fishing Pond Adult": ... has_pond_fish(ADULT_FISH, 8, 25) || has_pond_fish(ADULT_LOACH, 29, 36)
```

y la firma es `has_pond_fish(ageAndType, minPounds, maxPounds)`, comprobado en
`packages/logic/src/expr/parser.ts`. O sea: **child desde 7 libras, adult desde
8**. Por debajo, relleno.

**Los loaches nunca son basura**, y no hace falta ninguna regla: sólo existen
desde 14 (child) y 29 (adult), que son sus propios umbrales.

El peso se **parsea**, no se mete en los dígitos de un patrón, para que una
versión que añada pesos nuevos no los reclasifique en silencio.

### Lo que cambia, medido en `dHN9YY2c`

| | |
|---|---|
| Ubicaciones que pasan a basura | **66** de 5.981 |
| de ellas `Piece of Heart` | 44 |
| de ellas peces por debajo del umbral | 22 |
| Peces que **siguen contando** | 11 (de 8 libras para arriba, más el alma y el `Fish` suelto) |
| «Lo que importa» | 1.382 → **1.316** |

### (hecho de paso) Los paneles sueltos salían recortados en OBS

`body.panel` sólo levantaba el tope de altura de `.pane-tall` y `.pane-half`.
`regions`, `remaining` y `activity` son `.pane-third`, así que servidos por su
cuenta se quedaban con `calc((100vh - 604px) / 3)`: **158 px en una fuente de
1080**, y el suelo de 90 px por debajo de 604 px de alto. Parecían estar
escondiendo cosas, y las escondían. Ahora la regla lista las tres clases.

## (resuelto) El tracker enseñaba los items de otra seed

**Cazado el 14 ago 2026**, jugando `dHN9YY2c` mientras el tracker mostraba
`Circus Leader's Mask` en el cofre de la espada Kokiri. En el juego no estaba.

**No era de lectura: era de detección.** Las dos ROMs de esa seed multiworld
dan lo correcto, y cada una casa con su mundo del spoiler:

| ROM | cofre según la ROM | spoiler |
|---|---|---|
| `dHN9YY2c.z64` | gi 42, player 2, `Hylian Shield` | mundo 1: `Player 2 Hylian/Hero Shield` |
| `dHN9YY2c (1).z64` | gi 76, player 2, `Green Rupee` | mundo 2: `Player 2 Green Rupee` |

Los cinco items que se veían en pantalla eran, uno por uno, los de
**`dockiNAq`**: el overlay estaba usando su `checks.json`.

### Por qué, y por qué no dijo nada

`find_rom()` elegía la ROM por **la carpeta de partidas escrita más
recientemente**. Y esa recencia no responde a la pregunta que hace falta:

- `dockiNAq` → `.fla` del 14 ago 10:36
- `dHN9YY2c` → `.fla` del 13 ago 22:13

Ganaba `dockiNAq`. Como `checks.json` ya era suya, `ensure_tables()` no
regeneraba, y no hay barrera más abajo que lo pille: **otra seed de la misma
versión tiene exactamente las mismas ubicaciones con otros items**, cosa que
el propio `check_spoiler()` de `overlay.py` ya avisaba que no puede detectar.
El fallo silencioso de manual.

### El arreglo

El emulador sabía la respuesta desde el principio, en su propio `.cfg`:

```
Recent Rom 0 = C:\Users\<usuario>\Downloads\OoTMM-dHN9YY2c (1).z64
```

Las dos señales contestan preguntas distintas y ahora se usan como lo que son:

- **`Recent Rom 0`** = la última ROM **abierta**. Es lo que hay en pantalla, y
  **manda**.
- **La partida más reciente** = la última ROM **guardada**. Va por detrás:
  hasta que guardas, sigue nombrando la seed anterior. Queda de **contraste**.

Y cuando no coinciden **lo dice**, con las dos rutas y diciendo cuál usa.

### De regalo, el multi quedó validado

Nunca se había probado. Comparando la colocación leída de cada ROM contra su
mundo del spoiler:

| | mundo 1 | mundo 2 |
|---|---|---|
| Ubicaciones comparadas | **5.018 / 5.018** | **5.018 / 5.018** |
| Campo `player` equivocado | **0** | **0** |
| El item casa | 96,7% | 96,7% |

El 3,3% **no son items mal**: el spoiler escribe el nombre lógico y el tracker
el del juego (`Progressive Hookshot`→`Hookshot`, `1 Bomb`→`Bomb`,
`Milk`→`Lon Lon Milk`, `Silver Rupee (Ice Cavern - Scythe)`→`Silver Rupee (Ice
Scythe)`). El del juego es el que le sirve a quien mira la pantalla.

### Lo que queda por aquí

El aviso va **a la consola**, que en este proyecto es la interfaz. Pero este
fallo se descubrió mirando un cofre en el juego, no leyendo la consola.
**Pendiente**: que el overlay enseñe en pantalla de qué seed son los checks que
está usando. Es la diferencia entre avisar y que se vea.

### C. Probarlo entero, single y multi

Nunca se ha hecho una pasada completa. La parte multi está **sin tocar**: son
las preguntas P4 (¿admite P64-EM dos scripts Lua a la vez?) y P6 (¿el buzón
coop transporta también items locales?) del POC, las dos abiertas desde el
principio.

**P4 es bloqueante y hay que probarla antes de una sesión larga** (14 ago
2026, al preparar el `.exe` para una partida multi). El README tiene la prueba
de dos minutos en «En multiworld». Y ojo con una idea equivocada que casi se
cuela en la documentación: **el modo `proxy` NO es el respaldo**. `cmd_proxy`
sólo parsea el tráfico entre MultiClient y Lua y saca un resumen de las
direcciones que usa el multi; no toca el `Tracker` ni alimenta el overlay. Si
P64-EM no admite los dos scripts, hoy **no hay forma** de tener tracker y multi
a la vez, y haría falta un Lua que hiciera las dos cosas — que además tendría
dentro código del MultiClient, o sea que tampoco se repartiría tal cual.

Para single, lo que falta es una partida de verdad de punta a punta, no
volcados: cambiar de juego varias veces, guardar y recargar, y ver que las
bases se relocalizan y que el feed no miente.

### D. Probar con la versión de desarrollo de OoTMM

Para ver qué se rompe antes de que se rompa en directo. Las tres dependencias
de versión conocidas, en orden de fragilidad:

1. ~~**Las VROM de las tablas de xflags**~~ — **resuelto el 14 ago 2026**: se
   localizan por forma y las constantes quedan de contraste.
2. ~~**`data/gi.yml`**~~ — **resuelto el 14 ago 2026**: los nombres salen de
   `kItemNames[]` de la ROM, no de un fichero indexado por posición.
3. **La clave de `COMBO_VROM_CHECKS`** (formato y numeración de `ovType`).
   La dirección sí es estructural y debería aguantar.

Hay un `OoTMM-Lunes/` con una seed `dev-adc3993` en `Downloads`, que sirve
justo para esto.

## Overlay

### 0. (resuelto) Los selectores de la vista de director no aplicaban en vivo

Los tres selectores —Mostrar, Spoiler, Fondo— sólo llamaban a `renderUrls()`:
regeneraban los enlaces de los paneles pero **no cambiaban la vista que
estabas mirando**. Ahora aplican en el sitio y además escriben la opción en la
barra de direcciones, así que recargar no la pierde. Ver el POC.

### 0b. (resuelto) Cargar el spoiler sin reiniciar

Botón en la vista de director. Antes el spoiler sólo entraba al arrancar, y
sin él no hay filtro de relleno ni se sabe qué hay en lo pendiente. Valida
versión, coincidencia de nombres y **cobertura** antes de aceptarlo, y al
cargarlo aplica las dos cosas a los paneles (casilla para desactivarlo).
Ver el POC.

### 1. (resuelto) Filtrar el relleno

`?junk=hide` o el selector **Mostrar** de la vista de director. De 4.995
checks a 612, y los pendientes de una zona de 73 a 2. Va por el item que hay
dentro, clasificado **en el servidor**, así que sigue funcionando con
`?spoiler=off`. Ver el POC.

### 2. Gestor de pistas: niveles de revelado

Hoy el spoiler es un interruptor de tres posiciones global
(`?spoiler=off|item|full`). La idea es convertirlo en un gestor de pistas con
botones, revelando por grados:

- sólo el juego (OoT / MM)
- juego + ubicación
- ubicación + objeto
- dato completo

Pensado para que el streamer se dé pistas a sí mismo en directo sin quemar la
seed entera de golpe, y para que el público vea el mismo nivel que él.

Cuando se haga, el mecanismo de `?spoiler=` ya está: sólo hay que ampliarlo de
tres niveles a los cuatro y añadir el control por check en vez de global.

### 3. (resuelto) Iconos de las máscaras de MM

Se extraen de la ROM de cada uno, con su arte real. Estaban en un archivo
CmpDma, con cada icono comprimido aparte con Yaz0, que es por lo que ningún
barrido de píxeles los encontraba. Ver el POC.

Las siluetas dibujadas se quedan en `overlay.html` como respaldo, por si con
alguna ROM no se localiza el archivo.

## Soportar más de una versión de OoTMM

**Medido el 14 ago 2026, y baja mucho de prioridad.** Se comparó la seed
`dev-542a121` (experimental, 14 ago) contra la v32.0: las tablas de xflags
salen **idénticas**, 0 diferencias en los 4.751 `bitpos` y en todas las
direcciones; sólo cambian los items, que es lo esperado de otra seed. Y el
`.fla` de esa partida da **7 bits encendidos, los 7 mapeados** a un check con
nombre coherente, ni un huérfano.

O sea: las VROM de `custom.h` **no se han movido** entre v32.0 y la dev de
agosto, y lo que rompió con una seed de mayo fue un salto de versión mucho
mayor. Sigue siendo una dependencia real, pero no es lo que hay que arreglar
para jugar la experimental.

Cuando toque, dos vías:

- **Tabla de constantes por versión**: sacar los `CUSTOM_XFLAG_TABLE_*_ADDR`
  del `custom.h` de cada tag de OoTMM y elegir según la versión de la seed.
  Falta ver cómo se averigua la versión de un `.z64` a secas. **Y el `.ootmm`
  no sirve**: su `meta.json` lleva `"version":"1.3"`, que es la del formato
  del patch, no la del generador, y su `symbols.json` sólo tiene ajustes
  cosméticos y de multi (`MUSIC_NAMES`, `DPAD_COLOR`, `MULTI_*`, tunicas) —
  ni tablas ni símbolos de código.
- **Localizar las tablas por contenido**, que es el método que ya se usa para
  todo lo demás. Medido abajo: funciona, y es más barato de lo que parecía.

### (resuelto) Localizar las tablas por forma

**Hecho el 14 ago 2026**, `locate_xflag_tables()` en `mkchecks.py`. Las
constantes de `custom.h` se quedan **como contraste, no como fuente**: si lo
localizado no coincide con ellas, lo dice y sigue con lo localizado.

Sobre las **29 ROMs** de `Downloads`. Lo que se vio:

- **Cada tabla es su propia entrada de la extra DMA, y sin comprimir.** No hay
  que buscar dentro de un fichero: son seis entradas seguidas.
- **La forma no ha cambiado nunca**: OoT `101 / 142 / 6252`, MM
  `114 / 118 / 4524`, en las 29, y eso cruzando las dos familias de versión.
- **La cadena se valida sola**: `scenes` y `setups` son u16 no decrecientes que
  empiezan en 0, y cada una indexa a la siguiente (`max(scenes) < len(setups)`,
  `max(setups) < len(rooms)`). Con ese criterio salen **exactamente dos cadenas
  por ROM y ningún falso positivo**.
- En las 12 ROMs actuales el localizador devuelve **justo las constantes
  cableadas** (`0x080B0F00` / `0x080B41D0`), o sea que el cambio sería un no-op
  comprobable. En las 10 viejas devuelve `0x080948D0` / `0x08097BA0`: sólo se
  habían movido `0x1C630`.
- `scenes` y `setups` son **byte a byte idénticas** entre las dos familias; sólo
  cambia el contenido de `rooms`, que es dato que se lee, no constante.

**Y lo que NO arregla, que es lo importante.** Con las tablas localizadas, la
seed vieja pasa de abortar a resolver los 4.751 xflags... pero aparecen **30
colisiones** (22 en OoT y 8 en MM), todas de `Boulder`. Los CSV del pool son de
v32.0 y esa versión no tenía esos actores, así que sus filas caen encima de
bits de otros checks. Localizar arregla **el direccionamiento, no el pool**.

Eso obligó a una **segunda barrera**, porque si no el cambio empeoraba las
cosas: antes una seed vieja abortaba y dejaba el `checks.json` bueno en su
sitio; con las tablas localizadas pasaba a escribir uno con 30 checks que se
marcan entre ellos. Ahora `collisions()` cuenta los pares que comparten bit sin
ser vanilla/MQ y, si hay alguno, aborta **antes de escribir** diciendo que lo
que no cuadra es el pool, no las direcciones.

Lo que queda por aquí es el **pool**: hasta que las filas salgan de la ROM (o
se elija el CSV por versión), las seeds viejas seguirán parándose en esa
segunda barrera. Que es lo correcto.

## Soportar otros emuladores

**Preguntado el 14 ago 2026.** Sí lo admite, y bastante mejor de lo que
parecería. Esto no es una estimación: se puede medir con `grep`.

### Cuánto ata a Project64-EM, exactamente

Todo el tracker habla con el emulador por **dos métodos**: `link.read` (2 usos)
y `link.read_block` (22). No hay ni una llamada más. Todo lo demás
—`mkchecks`, `placement`, `overlay`, `inventory`— trabaja sobre direcciones y
bytes, que son del juego, no del emulador.

La prueba de que la costura existe ya está escrita: **`fakelua.py`** es un
emulador de mentira, y el overlay entero corre contra él sin enterarse.

Atado a P64-EM sólo hay tres cosas:

| Qué | Tamaño | Qué usa |
|---|---|---|
| `Scripts/tracker.lua` | ~130 líneas | `memory.read_u8/u16/u32`, `socket.tcp/recv/send/sleep`, `binary.pack_*` |
| `discover.py` | la mitad del fichero | `Config/Project64.cfg`, `Recent Rom N`, el hash de `Save/OOT+MM COMBO-<md5>` |
| `ensure_lua()` | 40 líneas | que los scripts vayan en `<emu>\Scripts\` |

El `.fla` **no** cuenta: se usó para verificar el mapeo y no se lee en marcha.

### Las dos formas de añadir uno

1. **Un script dentro del emulador** que hable el mismo protocolo de 8
   opcodes. Es portar 130 líneas de Lua. Vale para cualquier emulador con
   scripting y sockets.
2. **Un adaptador en Python** que implemente `read` y `read_block` contra el
   mecanismo nativo del emulador. No hace falta scripting en el emulador, sólo
   alguna vía de leer memoria. Es la vía para lo que ya trae un protocolo de
   memoria hecho.

La 2 es la que conviene de estructura: convierte `Link` en una interfaz con
varias implementaciones, y `handshake()` deja de estar cableado a un socket.

### Candidatos, y qué hay que verificar de cada uno

**Nada de esto está comprobado**, son los sospechosos habituales y lo que
habría que mirar antes de prometer nada:

- **BizHawk** — el mejor candidato, y **es el que hay que probar** (pedido el
  14 ago 2026). Ver la tarea de abajo.
- **Project64 mainline (3.x/4.x)** — creo que su scripting es **JavaScript**,
  no Lua, así que sería portar el script a otro lenguaje, no reutilizarlo.
  Verificar antes de nada.
- **RetroArch / Mupen64Plus-Next** — no tiene scripting, pero su interfaz de
  red trae `READ_CORE_MEMORY`. Sería la vía 2, con Python de cliente.
- **simple64, ares** — ni idea de si exponen memoria. Mirar antes de gastar
  tiempo.

### Los tres gotchas que van a salir

1. **Espacio de direcciones.** Aquí se leen direcciones virtuales
   (`0x80000000`) y el emulador las resuelve. Otro puede querer el offset
   físico (`& 0x7FFFFF`). Es una resta por backend, pero si se olvida no falla:
   **lee otra cosa**.
2. **El orden de los bytes.** El `PING` ya resuelve el del protocolo —por eso
   `handshake()` prueba las dos—, pero el orden *dentro del dominio de memoria*
   del emulador es otra pregunta distinta. Y ya sabemos que en este proyecto
   los volcados word-swapped existen: el `.fla` lo está.
3. **La detección automática se pierde.** `discover.py` sabe de Project64 y de
   nadie más. No es bloqueante: `--rom` ya existe y hace el mismo trabajo a
   mano. Lo suyo sería un backend de detección por emulador y que el resto
   siga igual.

### Lo que no se puede prometer

- **El multi no viaja.** `adapter.lua` es del MultiClient y es de P64-EM. Un
  emulador nuevo daría single, y el multi sería otra conversación.
- La forma buena de plantearlo es **con dos emuladores a la vez, no uno**: con
  uno solo, la interfaz sale con la forma de ese emulador. Y el guardia ya
  está: si `/state.json` sale idéntico contra `fakelua.py` y contra el
  emulador nuevo, el backend está bien.

### Tarea: probar BizHawk

**Pedido el 14 ago 2026.** Es el primer emulador que se intenta, así que sale
de aquí tanto el soporte como la forma que tenga que tener un backend.

Por orden, y **parando en el primero que falle**, que es lo que dice si esto
vale la pena o no:

1. **¿Se lee memoria?** Un Lua de tres líneas en BizHawk que imprima
   `osMemSize` (`0x80000318`, tiene que dar `0x00400000` o `0x00800000`) y la
   firma del save context. Si esto sale, el resto es fontanería.
2. **¿Direcciones virtuales o físicas?** Es la pregunta que decide todo lo
   demás. Si el dominio es `RDRAM`, seguramente haya que restar `0x80000000`;
   si hay un `System Bus`, quizá no. **Comprobarlo, no suponerlo**: leer el
   mismo dato de las dos formas y ver cuál casa con `ram-en-oot.bin`, que ya
   sabemos lo que tiene dentro.
3. **¿En qué orden salen los bytes?** Leer un u32 conocido y compararlo con el
   volcado. Ojo: que un `read_u32_be` dé lo correcto no garantiza que leer 4
   bytes sueltos los dé en ese mismo orden. Esto ya mordió con el `.fla`.
4. **¿Puede conectarse a un socket?** El tracker escucha y el script llama, y
   esa forma no se quiere cambiar. Si el Lua de BizHawk no puede abrir un
   socket saliente, la vía es la 2 (adaptador en Python) y hay que replantear.
5. **Portar `tracker.lua`**, que son 130 líneas y ocho opcodes.

El guardia final, que es gratis: arrancar el overlay contra BizHawk con una
partida, y contra `fakelua.py` con un volcado de esa misma partida. Si
`/state.json` no sale igual, el backend miente en algo.

Lo que hay que evitar es lo de siempre en este proyecto: si las direcciones se
traducen mal, **no falla, lee otra cosa** y el overlay reporta progreso en
zonas donde no has estado. Antes de dar nada por bueno, mirar la confianza y
que los checks encendidos tengan nombres coherentes.

## (resuelto) La seed experimental

**Hecho el 14 ago 2026**, con el volcado `ram-dev.bin`. Era la distancia al
custom save, que era una constante y resultó ser **de la versión**: en
`dev-542a121` pasó de `0x8A8` a `0x8D8`, el ancla caía `0x30` por delante,
confianza 0.077, y el ancla `custom` entera se descartaba — 4.751 xflags y 506
bitmaps sin contar. Ahora se mide barriendo una ventana, con confianza como
criterio y alineación a 16. Ver el POC.

**Cerrado por los dos lados.** La predicción era que, si el bloque creció
`0x30` por la cola, con MM corriendo la distancia pasaría de `0x8A0` a `0x8D0`.
Medido sobre `ram-dev-mm.bin`: **`0x8D0`**, confianza 1.0 y 18 checks, frente a
0.167 y 4 de basura con la constante vieja.

Las cuatro bases de save de la dev están ahora en `KNOWN_BASES`, así que
localizarlas ya no dispara el escaneo de 8 MB por el enlace: el primer sondeo
de los volcados de la dev pasó de **8,42 MB a 0,02 MB**.

## Rupias y corazones que no son los de la partida

Visto el 14 ago con la seed experimental: el resumen daba `rupees 150`,
`hearts 224`, `max hearts 224` en una partida que iba por 48 (3 corazones).
Esos tres salen del save de **OoT** aunque estés jugando MM, porque OoTMM los
comparte.

**Sin confirmar.** Al mirarlo salió un agujero real y ya tapado —las bases se
elegían de una en una y un buffer viejo puede validar, ver el POC— pero **no
está demostrado que fuera la causa**: en los cuatro volcados que hay, la base
equivocada no llega a validar.

Para cerrarlo hace falta **un volcado en el momento en que se vean los valores
raros**. Lo que hay que mirar ahí es qué bases elige `locate_saves` y si el par
es coherente:

```
python ootmm.py dump 0x80000000:0x800000 --out ram-raro.bin
```

Pista para entonces: `0x801C6954` lleva firma de MM con `cap=128 health=128`,
o sea datos de otra partida, y sólo lo descarta el tope de skulltulas. Hay
buffers así por la RAM y son justo lo que puede colarse.

## `server not responding` en el overlay

Visto una vez el 14 ago con la seed experimental. Ese mensaje sale **sólo si
falla el `fetch` de `/state.json`**, no si falla el sondeo —eso pondría
`state.error`—, así que es el servidor HTTP el que no contesta, no el enlace
con el emulador.

Sin reproducir. El sospechoso era el escaneo de 8 MB en el primer sondeo, que
ya no ocurre (arriba). Si vuelve a salir, hay que anotar **si es momentáneo al
arrancar o persistente**, que es lo que separa «el servidor aún no estaba
levantado» de un fallo de verdad.

## Lo que sigue atado a la versión, y si se nota

Repasado el 14 ago 2026 a raíz de lo anterior. Lo que **se localiza solo**:
las bases del save (firma), el `PlayState` (firma de `GameState`) y la
distancia al custom save (ventana medida). Lo que sigue siendo constante:

| Qué | Si cambia | ¿Se nota? |
|---|---|---|
| `+0x380`, dónde empieza `MmCustomSave` | todo lo de MM se desplaza | sí, la confianza cae. **Verificado que aguanta en la dev**: `+0x376` y `mm.halfDays` en `+0x6F4` están donde el layout de v32.0 los pone, así que los `0x30` que creció el bloque son de cola |
| ~~VROM de las tablas de xflags~~ | ~~bitpos basura~~ | **RESUELTO el 14 ago 2026**: se localizan por forma; las constantes son el contraste |
| ~~**CSV del pool**~~ | ~~checks que se marcan entre ellos~~ | **RESUELTO el 14 ago 2026**: el censo sale de la ROM y el CSV es diccionario de nombres; una fila que la ROM no lista sólo se queda el bit si está libre |
| ~~`data/gi.yml`~~ | ~~nombres de items corridos~~ | **RESUELTO el 14 ago 2026**: los nombres salen de `kItemNames[]` de la ROM |
| ~~`scenes.yml`~~ | ~~checks que faltan o sobran~~ | **RESUELTO el 14 ago 2026**: una escena que falte se localiza en la ROM por sus actores; probado quitando tres, `checks.json` sale byte a byte idéntico |
| La ventana `0x800`–`0x1000` | vuelve a fallar | sí, `trusted: false` |

**Ya no queda ningún fallo de versión silencioso, ni ningún atado sin
localizar.** El último de cada cosa se cerró el 14 ago 2026: `gi.yml` leyendo
los nombres de la ROM, los CSV del pool pasando el censo a la ROM, y
`scenes.yml` localizando la escena que falte por sus actores.

## (hecho) `scenes.yml`, localizado por contenido

**Hecho el 14 ago 2026**, `recover_scene_ids()` en `mkchecks.py`. Salió al
probar el paso 4: era el último fichero con una constante de versión dentro, y
fallaba de la peor manera posible. Una fila cuya escena `scenes.yml` no conoce
**no forma clave**, así que no se puede casar con la ROM; emitirla igual
**duplica** cada check de esa escena (uno sin nombre desde la ROM, otro sin
dirección desde el CSV) y saltarla **hace desaparecer checks**.

El id se recupera por el mismo truco que las tablas de xflags y `kItemNames`:
**por contenido**. La clave es `(ov << 24) | (escena << 16) | resto`, y el CSV
sabe todo menos la escena — así que se le quita el byte de escena a las filas
de un nombre y se busca bajo qué id lista la ROM justo ese conjunto.

| prueba | resultado |
|---|---|
| Quitar `MM_GROTTOS` (452 checks, la escena mayor) | id `0x07`, 438/448 filas, **segundo candidato 0** → `checks.json` **byte a byte idéntico** |
| Quitar `OOT_HYRULE_FIELD` (178) | id `0x51`, 174/174 → **idéntico** |
| Quitar `OOT_LINK_HOUSE` (1) | id `0x34`, 1/1 → **idéntico** |
| Vaciar `scenes.yml` **entero** | recupera **66 de 210**, y **0 mal** |

Lo importante de la última fila: cuando no puede, **no se inventa una**. Exige
cubrir el 90% de las filas del nombre y sacarle un 50% al segundo candidato; si
no llega, lo dice y esas filas salen sin dirección. Vaciarlo del todo no es un
escenario de versión, es destruirlo: ahí sí degrada (7.041 checks, con los
duplicados que se explican arriba), y lo avisa con dos contadores.

### El defecto que destapó, y que era mío

El paso 3 saltaba las filas sin clave (`continue`). Medido al quitar
`MM_GROTTOS`: **10 checks desaparecían** —los solo-CSV de esa escena, que la
ROM no devuelve—. Antes salían como pendientes, así que el cambio era **peor
que lo que reemplazaba**, que es justo lo que avisa [[ootmm-fallos-silenciosos]].
Ahora `check_without_key()` las emite sin dirección y con `no_key: true`. En
todos esos casos nunca hubo dirección que perder: los tipos que llevan escena
la necesitan para direccionarse, y los que no, necesitan un id que a esa fila
le falta.

## (hecho) El pool, de la ROM

**Planteado y hecho el 14 ago 2026**, los cinco pasos. Era lo que quedaba para
que el tracker fuera autosuficiente: los CSV del pool eran la lista de qué
checks existen, y son de v32.0.

**Dónde quedó:** el censo lo pone la ROM (`COMBO_VROM_CHECKS`), los CSV son un
diccionario de etiquetas indexado por `ovkey`, y cuando dos checks quieren el
mismo bit manda el que la ROM lista. Resultado medido: `checks.json` **byte a
byte idéntico** en las seeds actuales, y **la seed vieja `Siixg4Kf`, que
abortaba, ahora genera** con 0 colisiones.

Lo que sigue sin salir de la ROM son cinco etiquetas —nombre, región, item
vanilla, tipo fino del xflag y la escena de los siete de id global— y ninguna
llega a una dirección: lo peor que puede pasar ya es que un check salga con
nombre feo.

Que conste de entrada: **esto no arregla ningún fallo**, gana compatibilidad
hacia adelante. Lo urgente se cerró con la barrera de colisiones — una seed de
otra versión ya se para y dice que lo que no cuadra es el pool.

### Lo que se midió, y que dice que se puede

Sobre la seed `dockiNAq`:

| | |
|---|---|
| Claves en `COMBO_VROM_CHECKS` | **5.018**, y `checks.json` las cubre todas: **cero huérfanas** |
| Bit recalculado **sólo** desde la clave de la ROM | **4.751 de 4.751**, ni uno mal |
| Filas del CSV que la ROM no lista | 672 = **616 MQ** + 56 |

Lo importante del segundo: la clave lleva dentro `ovType`, escena, sala, setup
y actor, o sea **todo lo que necesita el cálculo del bit**. Con las tablas ya
localizadas por forma, el bit sale entero de la ROM sin tocar el CSV.

Se comprueba así, que es la prueba a repetir antes de tocar nada:

```python
ov, escena, room_byte, actor = key >> 24, (key >> 16) & 0xFF, (key >> 8) & 0xFF, key & 0xFF
slice_, room, setup = ov - placement.OV_XFLAG0, room_byte & 0x3F, room_byte >> 6
packed = (slice_ << 16) | ((setup & 3) << 14) | (room << 8) | actor
bit = tablas[game].bitpos(escena, packed)      # tiene que dar el bitpos de checks.json
```

### Lo que la ROM NO tiene

**Corregido el 14 ago 2026 al hacer el paso 2**: no son tres cosas, son cinco.
Todas etiquetas, ninguna llega a una dirección.

- **El nombre** (`Kokiri Forest Rock Circle Rock 6`), que es interno del
  randomizer.
- **La región** (el campo `hint` del CSV), que es lo que agrupa el panel de
  zonas.
- **El item vanilla**.
- **El tipo fino de un xflag** (`pot`, `grass`, `tree`). El `ov` sólo da el
  `slice`, y el slice es qué gota del actor es, no qué actor es: el slice 0 de
  OoT lleva 19 tipos dentro. Para los otros diez tipos el `ov` sí es exacto.
- **La escena de los siete de id global** (npc, gs, cow, shop, scrub, sr,
  fish): su clave lleva el byte de escena a 0. Da igual para direccionarlos,
  pero el panel de regiones la usa.

O sea: la ROM dice **qué hay y dónde está su bit**; el CSV sólo pone nombres.

### El diseño: unión, con la ROM de autoridad

No vale «manda la ROM y lo que no liste se tira», y esto es lo que lo decide:
de las 672 que no lista, 616 son **MQ** —esta seed no tiene mazmorras MQ, así
que esos checks no existen y tirarlos es lo correcto— pero las otras **56 sí
existen**: `Death Mountain Crater Silver Boulder`, `Lake Hylia Pot`… Se pueden
coger, con su item vanilla, y encienden su bit igual.

Así que:

- **Lo que la ROM lista** manda: existencia, tipo, escena y bit salen de ahí.
  El CSV entra sólo como **diccionario de nombres emparejado por `ovkey`**.
- **Lo que sólo está en el CSV** se conserva, y la barrera de colisiones es la
  que vigila que no meta basura.
- **Clave sin nombre** en el CSV → nombre sintético
  (`OoT · Kokiri Forest · pot #3`) **con el bit correcto**.

El modo de fallo se invierte: hoy un CSV de otra versión da bits equivocados;
después, lo peor que pasa es que unos checks salgan con nombre feo.

De regalo, **MQ saldría bien sin spoiler**: hoy `mq_scenes` es `null` sin él,
y la ROM lista lo que de verdad hay.

### Los pasos, en orden, con qué comprobar en cada uno

1. ~~**Un índice de nombres por clave.**~~ **HECHO el 14 ago 2026**, ver abajo.
2. ~~**Construir los checks desde las claves de la ROM.**~~ **HECHO el 14 ago
   2026**, ver abajo. No hizo falta reordenar nada.
3. ~~**Añadir las que sólo están en el CSV**~~ **HECHO el 14 ago 2026**, con la
   regla de prioridad por bit, ver abajo.
4. ~~**Nombres sintéticos** para claves sin fila.~~ **HECHO el 14 ago 2026**,
   con la prueba forzada. Ver abajo.
5. ~~**Probar con una seed vieja** (`Siixg4Kf`)~~ **HECHO el 14 ago 2026**: ya
   no aborta, y salió de la propia regla del paso 3. Ver abajo.

### (hecho) Paso 1: el índice de nombres por clave

**Hecho el 14 ago 2026.** Tres funciones nuevas en `mkchecks.py`, todas
aditivas: `row_parts()` (la mitad de una fila que la ROM también sabe, sacada
de `main()` para que haya **un solo sitio** donde se lee una fila del pool),
`name_index()` y `index_coverage()`.

Guardia del refactor: `checks.json` regenerado con `dockiNAq` sale **byte a
byte idéntico** (sha256 `a4f68a52…`, 3.461.466 bytes).

Lo medido, con las tres predicciones que se hicieron antes:

| | predicho | medido |
|---|---|---|
| Filas del CSV con clave | todas | **6.043 de 6.043, 0 sin clave** |
| Claves huérfanas (ROM sin fila) en `dockiNAq` | 0 | **0** de 5.018 |
| Ídem en la seed activa `f5PCTnhD` | — | **0** de 5.018 |
| Ídem en la seed vieja `Siixg4Kf` | — | **0** de 4.571 |
| Claves duplicadas dentro del CSV | ~600, todas vanilla/MQ | **353, todas pares vanilla/MQ limpios** |

**Y la aritmética cierra por los dos lados**, que es lo que lo da por bueno:
5.690 claves distintas, 969 filas MQ con clave, de las que 353 comparten clave
con su gemela vanilla → 969 − 353 = **616**, que son exactamente las 616 MQ que
la ROM no lista. Y 5.018 + 353 = **5.371**, que es clavado lo que
`placement.resolve()` viene contando como resuelto.

Que la seed vieja también dé **cero huérfanas** es el resultado importante: el
índice cubre una ROM de otra familia de versión sin tocar nada.

### (hecho) Paso 2: los checks se construyen desde la clave

**Hecho el 14 ago 2026.** `check_from_key()` en `mkchecks.py` construye un
check entero a partir de `(game, ovkey)`, y `main()` ya no arma ninguno a mano:
recorre las filas del CSV para conservar **el orden** y llevar las etiquetas,
pero **ni un campo que llegue a una dirección sale de sus columnas**.

`checks.json` sale **byte a byte idéntico** (mismo sha256 que antes del paso 1),
así que no hubo que reordenar ni comparar por `name`.

**Y la prueba que el guardia no ejerce**, porque hoy no hay huérfanas: se
construyeron las claves de la ROM **dos veces, con etiquetas y sin ninguna**, y
se compararon los doce campos de direccionamiento (`target`, `kind`, `field`,
`bit`, `csv_id`, `addr`, `bitpos`, `flash_off`, `anchor`, `off`, `xflag`,
`target_field`):

| seed | claves | campos que difieren sin etiquetas |
|---|---|---|
| `dockiNAq` | 5.018 | **0** |
| `f5PCTnhD` (la activa) | 5.018 | **0** |
| `Siixg4Kf` (la vieja) | 4.571 | **0** |

Y contra el `checks.json` que ya se daba por bueno, construyendo **sólo desde
la ROM**: **0 desacuerdos** en `addr`, `bit`, `bitpos`, `kind`, `target`,
`anchor` y `off`. La ruta sin `--rom` también sigue en pie (1.230 con
dirección, como antes).

Lo único que se pierde sin CSV es el `scene_id` de los **570** de id global
(npc, gs, cow, shop, scrub, sr, fish), y ahí es una **etiqueta**: esos siete
tipos no usan la escena para direccionarse.

#### Lo que se aprendió, y corrige el diseño escrito arriba

**El `slice` NO es el tipo.** El plan decía «tipo desde `ovType`», y para los
diez tipos con `ov` propio (chest, collectible, npc, gs, sf, cow, shop, scrub,
sr, fish) es exacto: `placement.OV` es invertible y cubre justo los diez de
`TYPE_TARGET`. Pero para los xflags el `ov` sólo da el `slice`, y medido: el
slice 0 de OoT lleva **19 tipos distintos** dentro (pot, grass, crate, tree,
wonder, boulder, icicle, redice…). El slice es **qué gota del actor** es, no
qué actor es.

No estorba, y conviene tenerlo claro: el tipo fino **nunca llega a una
dirección** —todos los xflags van al mismo target— y sólo lo usa el panel para
imprimirlo (`overlay.py:905`, y ahí muere). Así que el tipo fino se queda como
etiqueta, al lado del nombre, la región y el item vanilla. La lista de la ROM
para el paso 4 es entonces **nombre, tipo fino, región, item vanilla y la
escena de los siete de id global**.

### (hecho) Pasos 3 y 5: prioridad por bit, y la seed vieja ya genera

**Hecho el 14 ago 2026.** `apply_bit_priority()` en `mkchecks.py`, y `main()`
ya lee de la ROM qué claves existen (`claves_rom`) para saber cuáles son
solo-CSV. La regla que se acordó, que es «la ROM manda» bajado hasta el bit:

- Un check **que la ROM lista se queda con su bit, siempre**.
- Una fila **solo-CSV** se lo queda **sólo si está libre**.
- Si dos filas solo-CSV quieren el mismo bit, **ninguna se lo lleva**: nada
  dice cuál de las dos es la buena, y marcar el nombre equivocado es peor que
  enseñar las dos como pendientes.
- Compartir entre vanilla y su gemela MQ sigue siendo legítimo, igual que en
  `collisions()`.

Lo que se pierde es **la dirección, no el check**: vuelve a la forma de un
xflag sin resolver y se le añade `bit_taken_by` con quién se lo llevó. Y la
cuenta se imprime siempre, incluso cuando es cero.

**Los resultados, y el del final es el que importa:**

| seed | filas que pierden el bit | colisiones que quedan | ¿genera? |
|---|---|---|---|
| `dockiNAq` | **0** | 0 | sí, `checks.json` **byte a byte idéntico** |
| `f5PCTnhD` (la activa) | **0** | 0 | sí, 5.981 con dirección |
| `Siixg4Kf` (la vieja) | **46** | **0** | **sí — antes abortaba** |

De los 46 de la seed vieja: **los 46 son solo-CSV** (ninguno tiene item de la
ROM, que es la comprobación independiente), **36 son `boulder`** —que era
exactamente el diagnóstico original— y en todos los casos ganador y perdedor
están **en la misma escena**, así que los nombres son coherentes. Sale con
5.935 direcciones y 108 pendientes, frente a las 5.981 y 62 de una seed actual.

Nota: se habían estimado 45 y salieron 46. La diferencia es el segundo barrido,
el que quita el bit también al que sólo lo tenía por haberse mirado antes.

### (hecho) Paso 4: nombres para lo que el CSV no nombra

**Hecho el 14 ago 2026.** `scene_names()` invierte `scenes.yml` (210 entradas →
210 pares `(juego, id)`, **cero ambiguos**) y `synthetic_name()` compone el
nombre. El separador es `" · "`, que **ninguna fila del pool usa**, así que un
nombre inventado no puede chocar con uno real — y el overlay archiva todo por
nombre. En el JSON sale escapado (`·`), así que `checks.json` **sigue
siendo ASCII puro**.

No dice `pot`: la ROM no sabe el tipo fino, así que dice lo que sí sabe.

```
OoT · Link House · room 0 · actor 2      (un xflag)
OoT · npc 88                             (uno de id global, sin escena)
```

**La prueba forzada, que es la que ninguna seed ejerce.** Se copió `data/` a un
temporal, se borraron dos filas de `pool_oot.csv` —un xflag (`Link's House
Pot`) y un npc (`Hatch Chicken`)— y se regeneró contra `dockiNAq`:

| | |
|---|---|
| Checks totales | **6.043**, los mismos (6.041 filas + 2 huérfanas) |
| Huérfanas detectadas y avisadas | **2** |
| Campos de dirección que difieren, el xflag | **ninguno** de los 15 |
| Ídem el npc | **1**: `scene_id`, y sólo ése |
| Item leído de la ROM | correcto en los dos |
| Nombres duplicados en todo el fichero | **0** |

El `scene_id` del npc es el límite conocido y no tiene arreglo: los siete tipos
de id global llevan el byte de escena a 0 en su clave. La dirección sale bien
igual, porque no la usan.

**Y de paso salió un fallo latente.** El overlay ordena las barras de regiones
por `c["scene"]` (`overlay.py:171`), y un `None` entre cadenas hace saltar un
`TypeError`. Hasta ahora no podía pasar, porque toda fila del CSV trae escena;
en cuanto una huérfana de esos siete tipos apareciera, el panel se caía. Ahora
ese campo **siempre es una cadena**: el nombre de `scenes.yml`, o `SCENE_xx` si
el id no está, o `UNKNOWN` si no hay ni id.

### (resuelto así) El paso 3 chocaba con el paso 5

**Visto el 14 ago 2026 al medir el paso 1**, y hay que decidirlo antes de
escribir el paso 3.

El diseño de arriba dice *conservar lo que sólo está en el CSV*. Pero en
`Siixg4Kf` eso son **503 filas no-MQ**, y son justo las que producen las 30
colisiones: de los 60 checks implicados, **45 son solo-CSV**, y cada uno de los
30 pares tiene al menos un miembro solo-CSV. Medido: **tirando las solo-CSV
quedan 0 colisiones**. O sea que tal y como está escrito, el paso 3 devuelve el
problema que el paso 5 daba por resuelto.

La salida que no obliga a elegir entre las dos cosas es una **regla de
prioridad por bit**, que además es el mismo principio de «la ROM manda»:

- Lo que la ROM lista **se queda con su bit**, siempre.
- Una fila solo-CSV entra **sólo si su bit está libre**. Si lo quiere un check
  que la ROM sí lista, la fila se queda sin dirección (pendiente, no basura).
- Dos filas solo-CSV que quieran el mismo bit: ninguna se lo lleva.

Con eso `dockiNAq` no pierde nada —hoy no tiene ni una colisión, así que las 56
conservan su bit— y `Siixg4Kf` sale a cero. **Sin verificar**: hay que medirlo
al escribir el paso 3, no darlo por hecho.

### (resuelto) Los 56: el generador los quita a mano, uno a uno

**Cerrado el 14 ago 2026** leyendo el repo, que es lo que dice
[[ootmm-metodo-caza]] que hay que hacer antes de la quinta heurística.

El mecanismo, y explica los 56 enteros:

1. **`packages/generator/scripts/xsanity.ts` genera los CSV** enumerando los
   actores de cada escena. Es un volcado en bruto: si hay un árbol, sale una
   fila. Por eso el CSV es un **superconjunto**.
2. **`packages/logic/src/world/transform.ts` quita ubicaciones a mano**, según
   los ajustes y la lógica, con llamadas `removeLocations([...])` una por una.
3. Sólo las que sobreviven reciben override, y por eso sólo ésas están en
   `COMBO_VROM_CHECKS`.

Las retiradas con nombre y motivo, copiadas del fuente, casan con lo que
salió al agrupar los 56 por nombre:

| en `transform.ts` | qué quita | de los 56 |
|---|---|---|
| `/* Can't reach Gorman track trees */` | los `tree` de `GORMAN_TRACK` | **25** |
| `/* Impossible boulders */` | `Death Mountain Crater Silver Boulder 1..3` | **3** |
| `/* Carpenters */` | `Gerudo Fortress Jail 1..4` | **3** |
| `/* 100 skulls */` | `Skulltula House 100 Tokens` | **1** |
| `/* Can't reach this tree */` | `Hyrule Castle Tree Guarded` | — |

Y los que ya se declaraban en su propio nombre —`Unreachable`, `Out of
Bounds`, `JP Line`— son lo mismo por otra vía.

**Las dos lecturas que se barajaban eran la misma cosa.** Sí es por ajustes,
como decía el usuario, pero aplicado **por ubicación y no por categoría**: de
ahí que pareciesen fracciones dentro de cada tipo. `filterLocationsBool(...,
'tree', 'oot')` apaga la categoría entera; `removeLocations(['OOT Skulltula
House 100 Tokens'])` quita una sola.

**Y decide la duda que quedaba abierta.** Esos 56 **no son checks de esta
seed**: el generador los ha quitado porque no se pueden coger. Así que la frase
«se pueden coger y encienden su bit igual» era falsa, y conservarlos es una
cortesía, no una necesidad — que es exactamente por lo que la regla de
prioridad por bit los deja entrar sólo si el bit está libre.

### (histórico) La pista que llevó hasta ahí

Esos **56** no se sabía por qué no están en la tabla. Lo descartado:

- **Ajustes del randomizer no activados** (idea del usuario, 14 ago): explica
  bien las ausencias de **categoría entera** —los botones de la ocarina o las
  almas de enemigos ni siquiera están en el pool— pero **para estos 56 no
  cuadra**: son fracciones dentro de cada tipo. 4 de 896 `pot`, 7 de 1271
  `grass`, 25 de 135 `tree`, 3 de 41 `boulder-silver`. Con potsanity apagada
  faltarían las 896.

**La pista, del 14 ago: mirarles el nombre.** Agrupados, casi todos se declaran
solos:

| | |
|---|---|
| `Gorman Track Tree 1..25` | 25 |
| `Deku Palace **JP Line** Grotto Grass/Butterfly` | 10 |
| `Gerudo Valley Crate Child **Unreachable**` | 4 |
| `Gerudo Fortress Jail`, `Death Mountain Crater Silver Boulder` | 3 + 3 |
| `Pinnacle Rock Rock **Unreachable**` | 2 |
| `Lake Hylia Pot`, `Hyrule Castle Pot` | 2 + 2 |
| `Snowhead Small Snowball Spring **Out of Bounds**` | 1 |
| `Skulltula House 100 Tokens`, y 4 sueltos más | 5 |

O sea **inalcanzables, fuera de límites y contenido de la versión japonesa**:
actores que el generador filtra de uno en uno, que era la tercera lectura.

Lo que faltaba por explicar eran los 25 `Gorman Track Tree` y `Skulltula House
100 Tokens`, y es lo que cerró la sección de arriba: los quita `transform.ts` a
mano, con el motivo escrito al lado.

## (resuelto) Leer la colocación de la ROM

`placement.py`. La tabla `COMBO_VROM_CHECKS` (`0xF0400000` build de OoT,
`0xF0500000` build de MM) da el item de cada ubicación sin spoiler: 4.939 de
los 4.995 checks con dirección, y la clasificación de relleno coincide con la
del spoiler en el **100%** de las 5.018 comparables. El botón de cargar
spoiler queda de respaldo. Ver el POC.

Cabos sueltos de esto:

- **56 checks con dirección se quedan sin item** (1,1%): 25 `tree`, 7 `grass`,
  5 `crate`, 5 `butterfly`, 4 `pot`, 3 `boulder-silver`, 3 `collectible`, 2
  `rock`, 1 `snowball`, 1 `npc`. Cuentan como importantes, que es el lado
  seguro, pero habría que ver por qué su clave no está en la tabla.
- ~~`data/gi.yml` es de v32.0~~ → **resuelto**, ver abajo.
- Clasificar el relleno por **`item_id`** (el símbolo `OOT_RUPEE_GREEN`) en vez
  de por el nombre. **Ojo, esto ha cambiado de signo**: el símbolo es lo único
  que sigue saliendo del fichero, así que clasificar por ahí volvería a atar el
  filtro a la versión. Los nombres ahora vienen de la ROM y son fiables. Si se
  hace, que sea por el `gi` (el índice, que es lo que dice la ROM), no por el
  símbolo.

## (resuelto) Los nombres de items salen de la ROM

**Hecho el 14 ago 2026**, y con esto se cierra el último fallo de versión
silencioso. Los nombres salen de `kItemNames[]`, que vive en el payload
(`COMBO_VROM_PAYLOAD`, otro fichero de la extra DMA), y es
`const char* const kItemNames[]` indexado por `gi - 1`. Detalle en el POC.

Lo importante: **no era teórico**. De los 29 ficheros `.z64` que hay en
`Downloads`, **17 tienen 829 nombres en vez de 936** y `gi.yml` sólo acierta 136 de 822 en
ellas. Con esas seeds el tracker decía «Dungeon Map (Jabu)» donde había
«Compass (Water)», y «Soul of Lulu» donde había «Nayru's Love», sin un aviso.
Y hasta con la seed que se está jugando había 26 nombres mal, los
`Rusty Key (...)`, que no se notaban porque esa función no está activada y
ninguno de esos 26 se coloca.

`data/gi.yml` se queda **sólo para el símbolo** (`OOT_BOMBS_5`), que es un
símbolo del build y no sobrevive a la compilación. Y se usa nada más si sigue
alineado con la ROM: se comparan los nombres, y por debajo del 90% de acuerdo
se tira el `item_id` y se dice por qué. Con un item metido en medio a mano, el
acuerdo cae de 901/927 a 29/928 y lo canta.

Cabo suelto: esto arregla los **nombres**, no las tablas de xflags. Esas 17
siguen abortando en `mkchecks` con un 72% de bits imposibles, que
es el otro trabajo (localizar las tablas por contenido).

## Sistema de checks

- Confirmar en juego el `perm` de MM (`0x8044BF08`) y el `gsFlags` de OoT
  (`0x8011B46C`). Derivados por cuenta de estructura, aún sin un dato que los
  toque. Predicciones concretas en el POC.
- Los **62** checks que faltan: `caughtFishFlags` (33) y stray fairies de MM
  (29). Los 18 `cow_flags` se resolvieron el 14 ago: el macro
  `SAVE_EXTRA_RECORD` mete los registros propios de OoTMM en el campo `unk` de
  la tabla de escenas de OoT, así que `gCowFlags` es `oot_base + 0x1E0`. Ver
  el POC. **Pendiente de confirmar en juego**: al ordeñar la vaca de atrás de
  la gruta de Termina Field debe encenderse el bit 20.
- De la misma tabla quedan sin usar `gMmOwlFlags` (índice 11) y los cinco
  `gOotSilverRupeeCounts` (13–17), por si sirven para algo más adelante.
- Validar con una segunda seed.
- P4 (dos scripts Lua a la vez) y P6 (buzón coop), la parte multi.
