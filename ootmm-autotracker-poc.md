# POC — Autotracker OoTMM

**Timebox: 4–6 h.** El objetivo no es construir nada usable, es responder a las preguntas binarias que bloquean el diseño. Si algo se alarga, anótalo y sigue.

---

## Nota sobre el emulador

**La plataforma destino es Project64-EM, también para single player.** La comunidad de OoTMM está ahí (la wiki recomienda P64-EM, el P64 preconstruido de Maro, RetroArch y Ares; BizHawk no aparece). Un overlay para BizHawk sería un overlay para nadie.

Dos consecuencias:

- **Los offsets son independientes del emulador.** La base del save context es una propiedad de la ROM. Lo que descubras en cualquier emulador que la ejecute vale en todos. Lo único específico es la API de Lua y el orden de bytes del dominio de memoria.
- **La API de Lua de P64-EM la sacas del script del multi.** No hay documentación pública. Ese script es tu referencia y hay que leerlo antes de escribir una sola línea.

BizHawk es opcional y solo como comodidad de desarrollo: desde la 2.9 lleva el core Ares64 y OoTMM recomienda Ares, así que la ROM debería arrancar. Si en 15 minutos no lo consigues, no insistas y haz todo en P64-EM.

---

## Preguntas que este spike debe responder

- [x] **P1** — ¿Puedo leer memoria del juego desde Lua? **Sí.** Directamente en P64-EM, sin pasar por BizHawk.
- [x] **P2** — ¿Dónde está la base del save context, y es localizable? **Sí, y por firma.** OoT en `0x8011A5D0`, MM en `0x8044BE18`.
- [x] **P3** — ¿Existe un bitfield de checks completados? **Sí**, la tabla de flags por escena, con mapeo verificado contra el spoiler.
- [ ] **P4** — ¿P64-EM permite cargar mi script **a la vez** que el script del multi? Indicios de que sí; sin confirmar.
- [x] **P5** — ¿El Lua de P64-EM tiene sockets? **Sí.** `socket.tcp`, `send`, `recv`, `sleep`.
- [ ] **P6** (opcional) — En modo coop, ¿el buzón transporta también items locales o solo cruzados?

**P1, P2, P3 y P5 resueltas: el proyecto es viable en single player.** Queda P4/P6, que solo condicionan la parte multi.

> Todo lo de abajo se verificó sobre **OoTMM v32.0** (seed `f5PCTnhD`), en Project64-EM 1.0.3, con Expansion Pak. Sin tocar BizHawk en ningún momento.

---

## Preparación (30 min)

1. Genera **dos seeds single player con la misma versión de OoTMM**, distinta semilla. Guarda ambos spoiler logs.
2. Anota la versión exacta del generador. Todo lo que descubras a partir de aquí está atado a ella.
3. Clona `github.com/OoTMM/OoTMM`. Busca en el código las definiciones de save:
   - `grep -rn "gOotSave\|gMmSave\|gSharedCustomSave\|CustomSave" --include=*.h`
   - Mira si el build genera un `.map` o fichero de símbolos. Si lo hay, **has terminado la P2 sin tocar el emulador**.
4. Descomprime el release del `multi-client` y localiza el script Lua. Léelo entero antes de escribir nada tuyo — es tu documentación de la API real de P64-EM.

---

## Fase 1 — Leer algo (1 h)

> **RESUELTA.** Se hizo directamente en P64-EM. El ejemplo con `gui.text` de más abajo no es aplicable: no hay API gráfica. La verificación se hizo leyendo `osMemSize` en `0x80000318`, cuyo valor correcto (`0x00800000`) se conoce de antemano.

Objetivo: cerrar el bucle completo *dirección → lectura desde Lua → valor correcto en pantalla* con algo trivial como las rupias. Sin esto, nada de lo demás importa.

**Localizar las rupias.** Project64 arrastra un debugger con búsqueda en memoria, gestión de símbolos y breakpoints de lectura/escritura. La mecánica es la misma en cualquier herramienta:

1. Anota tu contador de rupias y busca ese valor exacto (2 bytes).
2. Gasta o recoge rupias y vuelve a filtrar por el nuevo valor.
3. Tres o cuatro iteraciones y te quedan pocos candidatos.
4. Escribe en uno de ellos y mira si cambia el HUD. Ese es el bueno.

> **Atajo:** los breakpoints de escritura son mejor herramienta que la búsqueda iterativa. Pon un watchpoint sobre un candidato, recoge una rupia, y ves directamente qué código toca qué dirección. Te será aún más útil en la fase 3.

**Leerlo desde Lua.** Saca las funciones exactas del script del multi. El equivalente en BizHawk, solo como referencia de la forma que debe tener el bucle:

```lua
-- REFERENCIA (API de BizHawk, NO de P64-EM)
local ADDR = 0x000000

while true do
  local v = memory.read_u16_be(ADDR, "RDRAM")
  gui.text(10, 10, "Rupias: " .. v)
  emu.frameadvance()
end
```

Anota en la plantilla de resultados los nombres reales de las funciones de P64-EM. Son la base de todo lo que escribas después.

**Conversión de direcciones:** las direcciones virtuales del juego (`0x80xxxxxx`) se traducen a offset físico con `addr & 0x00FFFFFF`.

> **Gotcha de endianness:** el dominio RDRAM puede exponer los bytes intercambiados en grupos de 4, y esto varía entre emuladores y cores. Si las lecturas de 16/32 bits cuadran pero las de 8 bits salen desplazadas, prueba `XOR 3` sobre la dirección para lecturas de byte. Confírmalo aquí, no más adelante.

✅ **P1 resuelta** cuando el número en pantalla siga al del juego en tiempo real.

---

## Fase 2 — Base del save context (1,5–2 h)

> **RESUELTA por una vía que no estaba prevista:** buscar la firma `ZELDAZ` en un volcado de RAM, y validarla contra el fichero `.fla`. No hizo falta ni la búsqueda iterativa de rupias ni el debugger.

Este es el riesgo real del proyecto.

**Vía A (preferida): desde la fuente.** Si sacaste la dirección del `.map` en la preparación, valídala leyendo un campo conocido (rupias, corazones) y comparando con lo que ves en juego.

**Vía B: por búsqueda.** La dirección de las rupias que ya tienes está *dentro* del save context. Dumpea el entorno a fichero y busca la cabecera:

```lua
-- REFERENCIA. Sustituye la lectura por la API real de P64-EM.
local BASE = 0x000000
local f = io.open("dump.bin", "wb")
for i = 0, 4095 do
  f:write(string.char(memory.read_u8(BASE + i, "RDRAM")))
end
f:close()
```

Ábrelo en un editor hex y busca la estructura: nombre de fichero, magic, contadores. Cruza con las structs del código.

**Validación obligatoria — no te saltes esto:**
- [ ] Lee 4–5 campos distintos (rupias, corazones, un upgrade, una canción) y verifica cada uno contra el juego.
- [ ] Repite con la **segunda seed**. Si la base cambia entre seeds de la misma versión, tu estrategia de offsets estáticos no vale y hay que localizar por firma en RAM.
- [ ] Guarda estado, recarga, comprueba que sigue valiendo.
- [ ] Comprueba qué hay en esa dirección en el **title screen**. Necesitas un chequeo de validez antes de emitir nada.

✅ **P2 resuelta** cuando leas correctamente en dos seeds distintas.

---

## Fase 3 — Bitfield de checks (1 h)

> **RESUELTA.** Existe, es la tabla de flags por escena, y el mapeo bit → check está verificado contra el spoiler. El paso 2 de abajo (diffear dos volcados) es lo que funcionó, pero solo tras acotar a las regiones que se persisten al fichero de save.

Es lo que separa un tracker de items de un tracker de progreso de verdad.

1. En el código, busca cómo marca OoTMM un check como recogido. Términos: `checks`, `locations`, `SAVE_BIT`, `setCheck`.
2. Pon un breakpoint de escritura sobre la zona candidata y abre un cofre: verás exactamente qué dirección se toca. Alternativa sin debugger: dumpea antes y después de abrir el cofre y diffea los dos ficheros. Buscas un bit que se pone a 1 y no vuelve.
3. Con el spoiler log delante, verifica que el índice del bit corresponde al check que has abierto.

Si aparece: el tracking de checks te sale casi gratis y el proyecto sube mucho de valor.
Si no aparece: sigues teniendo tracker de items perfectamente válido. No es bloqueante.

---

## Fase 4 — Convivencia con el script del multi (1 h)

> **PENDIENTE.** Único bloque sin tocar. No condiciona el single player.

Esto solo aplica a sesiones multi: en single player no hay script del multi corriendo y el problema no existe.

- [ ] Carga tu script mientras el script del multi está corriendo. ¿Conviven o se pisan?
- [ ] ¿Hay sockets en la API? Si no: escribe a fichero y que un watcher local lo siga.
- [ ] Si has usado BizHawk en las fases 1–3, revalida la base del save en P64-EM. Debería ser idéntica —misma ROM— pero confírmalo antes de darlo por bueno.

**Si no admite dos scripts:** no es fatal. La versión multi pasa a ser un fork del script del multi con tu emisor dentro, y mantienes un script propio e independiente para single player. Dos artefactos en vez de uno, mismo parseo detrás.

---

## Gotchas a verificar durante el spike

| Situación | Estado |
|---|---|
| Title screen / file select | **Confirmado**: la memoria es basura antes de arrancar. Se vio `0x00C8083C` → `0` → `0x00800000`. Flag de validez: `osMemSize` más la firma. |
| Cambio OoT ↔ MM | **El gotcha más peligroso de todos, y la primera respuesta que dimos era incompleta.** Es verdad que hay dos save contexts vivos a la vez. Pero al **cruzar** de un juego al otro, la RAM se reorganiza entera: el juego activo pasa a la zona baja y el otro a la alta. Con offsets fijos el tracker funcionaría perfectamente en OoT y empezaría a leer basura al entrar en MM, **sin dar ningún error**. La localización por firma lo resuelve sola, y de paso dice en qué juego está el jugador: el que tenga su firma en la zona baja. <br><br>`Jugando OoT:` OoT `0x8011A5D0` · MM `0x8044BE18`<br>`Jugando MM:` MM `0x801C6954` · OoT `0x8076C4F0` |
| **Latencia de los flags de check** | **Gotcha nuevo, no estaba en la lista, y es el que más afecta al diseño.** El flag de cofre no se escribe al abrirlo: se vuelca al save context **al salir de la escena** o al guardar. Un tracker que solo lea la tabla ve los checks con retraso. Para reacción inmediata hay que leer además los flags temporales de la escena activa (`+0x1357`–`+0x137B`). |
| Reset de ciclo de 3 días (MM) | Sin probar. El *latch* sigue siendo buena idea. |
| Save & quit + recarga | Parcialmente visto: los flags persisten entre volcados y sobreviven al guardado. Falta probar la recarga completa. |
| Frecuencia de polling | No aplica tal cual: **no hay callback de frame**. El bucle es libre, con `socket.sleep`. Un sondeo cada 100–250 ms sobra. |
| Lecturas a medias | Al no haber sincronía de frame, se puede pillar una estructura a medio escribir. No ha dado problemas leyendo el save context, pero conviene el *latch*. |

---

## Resultados

Esto es la entrada del `offsets/v32.0.json` del futuro demonio.

```
Versión OoTMM:        v32.0  (seed f5PCTnhD)
Emulador:             Project64-EM 1.0.3, Lua 5.4, RDRAM 8 MB (Expansion Pak)
u8 necesita XOR 3:    NO. read_u8 es coherente con read_u32.
Endianness protocolo: binary.pack_* es LITTLE endian, y la memoria del N64
                      es big endian. Los valores sueltos salen bien si
                      empaquetas y desempaquetas igual, pero los bloques de
                      bytes hay que invertirlos por palabras de 4.

Base save context:    OoT  0x8011A5D0      MM  0x8044BE18
  Método:             firma en RAM. "ZELDAZ" (OoT) / "ZELDA3" (MM) en base+0x1C.
  Copias estáticas:   OoT  0x800FBFB8      MM  0x80442248   (no usar: no cambian)
  Estable entre seeds:        SIN VALIDAR con una segunda seed.
                              Da igual: con búsqueda por firma deja de importar.
  Válida en title screen:     NO. Antes de arrancar hay basura.
                              Flag de validez: osMemSize en 0x80000318 == 0x00800000,
                              más la presencia de la firma.

Campos verificados (offsets relativos a la base de OoT):
  dayTime             +0x00C   u16   reloj del día; corre siempre, es ruido
  deathCount          +0x022   u16
  healthCapacity      +0x02E   u16
  health              +0x030   u16   16 por corazón; visto curar 44 -> 48
  magicLevel/magic    +0x032   s8+s8
  rupees              +0x034   s16
  swordHealth         +0x036   u16   durabilidad del Giant's Knife; sale a 8
  naviTimer           +0x038   u16   sube solo; también ruido
  items[24]           +0x074   ---   id del item por slot, 0xFF = vacío
  ammo[15]            +0x08C   ---
  beans               +0x09B   u8
  equipment           +0x09C   u16   4 nibbles: espadas/escudos/túnicas/botas
  upgrades            +0x0A0   u32   8 campos de 2-3 bits
  questItems          +0x0A4   u32   canciones, medallones, piedras
  dungeonItems[20]    +0x0A8
  dungeonKeys[19]     +0x0BC
  goldTokens          +0x0D0   u16
  flags de escena     +0x0D4   ---   ver abajo
  flags temporales    +0x1357..+0x137B   escena activa; cambian al cambiar de sala
  checksum            +0x1352   u16

  La estructura cierra exactamente en +0xD4, que es `perm`, verificado aparte
  con los cofres. Eso hace consistente el bloque de principio a fin.

  CORRECCIÓN de una versión anterior de este documento: +0x38 no es el reloj
  del día sino naviTimer (el reloj es +0x0C), y +0x537 NO es la magia (está
  en +0x33). Aquel +0x537 sigue sin identificar.

Bitfield de checks:  SÍ
  base    +0xD4 + escena*0x1C,  124 escenas de 0x1C bytes
  campos  +0x00 chest   +0x04 swch   +0x08 clear   +0x0C collect
          +0x10 unk     +0x14 rooms  +0x18 floors
  tamaño  0xD90 bytes en total
  Mapeo bit → check confirmado contra spoiler: SÍ
    escena 40 (0x28) chest = 0x0F  -> los 4 cofres de Mido's House
    escena 85 (0x55) chest = 0x01  -> Kokiri Sword Chest
  El campo `unk` no se usa en OoT vanilla y aquí sí lleva datos: es el
  candidato a que OoTMM guarde ahí los checks que no son cofres.

Regiones que se persisten al .fla (todo lo demás es volátil):
  0x800FBF00-0x800FC000   copia de OoT
  0x8011A5C0-0x8011BA20   save de OoT
  0x8044BE10-0x8044CE10   save de MM
  El .fla está word-swapped y el save de OoT empieza en su offset 0x20.

P64-EM:
  Dos scripts simultáneos:  SIN CONFIRMAR (el diálogo de Scripts es una lista
                            y el error es "script is already running", por
                            script y no global: apunta a que sí)
  Sockets en Lua:           SÍ, pero solo como cliente. El daemon tiene que
                            ser el que escucha.
  Lectura:      memory.read_u8/s8/u16/s16/u32/s32/f32/f64  (dirección 0x80xxxxxx
                cruda, sin dominio y sin máscara). Escritura: memory.write_*
  Empaquetado:  binary.pack_u8/../f64 y binary.unpack_u8/../f64
  Callback por frame:   NO EXISTE. Tampoco hay API gráfica (ni gui.text).
                        El script corre en su propio hilo, con bucle libre.
  Base del save igual que en BizHawk:  n.a., BizHawk descartado.
```

---

## Criterio de decisión

**Veredicto: adelante, y el spike se pasó de largo.** P1, P2, P3 y P5 resueltas, y encima salió de aquí un tracker que ya lee el inventario de los dos juegos en vivo. Lo que queda es diseño de overlay, no ingeniería inversa.

> **Estado en una línea:** se lee y se traduce a nombre, en vivo y para ambos juegos, el inventario completo (325 ids), canciones, medallones, piedras, máscaras, equipo bit a bit, mejoras, contadores y 713 checks. Sobrevive al cambio de juego. Lo que no se reconoce se reporta en crudo con su dirección.

P3 salió bien, así que el proyecto es un tracker de progreso de verdad y no solo de items.

El riesgo que se marcaba como letal —que la base del save se moviera de forma impredecible dentro de una misma versión— ha quedado cubierto por partida doble, y ni siquiera hizo falta esperar a que fallara:

- **Localización por firma**, que era el plan B, resultó ser trivial y es ahora el método principal. Los offsets son relativos a la base, así que un cambio de versión que mueva la estructura no rompe nada mientras la firma siga ahí.
- **Validación cruzada contra el fichero de save.** Los bytes de la RAM en `0x8011A5D0` son idénticos uno a uno a los del `.fla`. Eso convierte cualquier duda sobre la base en algo comprobable en segundos y sin emulador.

Queda pendiente validar con una segunda seed, pero con búsqueda por firma ha dejado de ser un riesgo de proyecto.

### Cosas del plan original que resultaron ser falsas

Vale la pena anotarlas, porque desviaron trabajo:

- **El `XOR 3` no aplica.** Es un gotcha de BizHawk. En P64-EM las lecturas de byte son coherentes con las de 32 bits. Sí hay un word-swap, pero está en el empaquetado del protocolo (`binary.pack_*` es little endian), no en la memoria.
- **La Fase 1 no se puede hacer como estaba escrita.** No hay `gui.text` ni ninguna API gráfica: el overlay no puede dibujarse dentro del emulador y tiene que ser una ventana externa. Para un overlay de streamer da igual, pero condiciona el diseño desde el principio.
- **No hay callback de frame.** El script corre en su propio hilo y el bucle es libre.
- **BizHawk era una pérdida de tiempo.** API distinta y un gotcha de endianness que no existe en el destino.
- **El bitfield no estaba donde se buscó primero.** Buscarlo con dumps de RAM completa da 22% de bytes distintos y es inviable. Lo que funciona es acotar a lo que se persiste al fichero de save: ahí el mismo experimento pasa de 46.000 bytes candidatos a 2.

---

## Siguiente paso si sale bien

El incremento que se propuso —Lua tonto que emite bytes, Python que parsea— **ya está construido y es lo que se usó para todo lo de arriba**. Con una diferencia: en vez de escribir a fichero va por socket, porque el Lua de P64-EM tiene sockets.

### Herramientas

**`Scripts/tracker.lua`** — servidor de memoria, independiente del script del multi. Puerto propio (13251) para poder correr los dos a la vez. Habla el mismo protocolo que `adapter.lua` en los opcodes 2/3/4/6/7/8, y añade `PING` (identifica el script y fija el orden de bytes) y `READ_BLOCK` (vuelca una región en una petición). Reconecta solo, así que se puede reiniciar el daemon sin tocar el emulador ni la ROM.

> **Vive en el proyecto, en `Scripts/tracker.lua`.** El emulador lo carga de su
> propia carpeta `Scripts\`, así que ahí hay un **enlace duro** al del
> proyecto: un solo fichero con dos nombres, sin copias que se desincronicen.
> Si alguna vez deja de estar enlazado —un editor que reescriba creando fichero
> nuevo en vez de truncar rompe el enlace— se rehace con
> `New-Item -ItemType HardLink -Path <ruta del emulador> -Target <ruta del proyecto>`,
> que no pide permisos de administrador porque los dos están en C:.
>
> Cuidado al tocarlo desde PowerShell: `Set-Content -Encoding utf8` en la 5.1
> escribe **BOM**, y tres bytes `EF BB BF` al principio le revientan el parser
> a Lua. Pasó al montar el enlace; se ve mirando los primeros bytes.

**`ootmm.py`** — siete subcomandos:

| | |
|---|---|
| `items` | **lee el inventario de ambos juegos en bucle y canta cada cambio** |
| `checks` | **lista los checks completados, por nombre**; en vivo o desde un volcado |
| `watch ADDR:SIZE,…` | sondea direcciones e imprime solo lo que cambia |
| `dump ADDR:LEN` | vuelca una región (acepta nombres: `oot`, `mm`) |
| `find dump.bin PATRÓN` | busca una firma; `--swapped` prueba las dos vistas |
| `diff a.bin b.bin` | compara volcados |
| `proxy` | captura qué direcciones usa el MultiClient |

**`mkchecks.py`** — genera `checks.json` cruzando los datos de OoTMM en `data/`. Con `--rom <seed.z64>` resuelve además los 4.751 xflags leyendo las tres tablas de lookup de la ROM; con `--spoiler` sabe qué mazmorras son Master Quest.

**`overlay.py` + `overlay.html`** — el tracker mirable. `ootmm.py overlay` levanta un hilo de sondeo, un servidor HTTP y una ventana propia. Ver la sección de abajo.

**`inventory.py`** — el mapa del inventario de los dos juegos, con la tabla de ids.

**`data/ref/`** — copias de los ficheros del repo de OoTMM en los que se apoya todo: `mark.c`, `xflags.c`, `items.h`.

**`Scripts/adapter-tracker.lua`** — clon del adapter con el puerto cambiado, solo para el modo `proxy`.

### El método que encontró el bitfield

Los filtros de `diff` importan más que la herramienta, porque el problema real es el ruido: un volcado de RAM completa da 22% de bytes distintos.

```
--exclude ruido.bin   descarta lo que cambia por sí solo (tiempo, RNG, actores)
--bits-set            solo bytes que ganan bits sin perder ninguno
--one-bit             solo bytes que encienden exactamente un bit
--max-run N           descarta rachas anchas, que son buffers
--range               acota a una región
```

El que de verdad resuelve es **acotar a las regiones que se persisten al `.fla`**. Todo lo demás es volátil por definición, así que un check no puede estar ahí. Con eso, el experimento del Recovery Heart pasó de 46.626 bytes candidatos a **2**.

Procedimiento reproducible para localizar un check nuevo:

1. Volcado `a`, con la partida cargada y el jugador quieto.
2. Volcado `ruido` diez segundos después, **sin tocar nada**.
3. Coger el item, **salir de la escena** (esto es imprescindible: el flag no se escribe hasta entonces) y volcado `b`.
4. `diff a b --exclude ruido` acotado a las tres regiones persistidas.

### Mapeo bit → nombre de check: **hecho** (para cofres)

La tabla no hubo que deducirla. Está en el repo de OoTMM, en tres ficheros:

| Fichero | Qué aporta |
|---|---|
| `data/pool/pool_oot.csv` | `location, type, hint, scene, id, item` para 3236 ubicaciones |
| `data/pool/pool_mm.csv` | lo mismo para 2807 de MM |
| `data/defs/scenes.yml` | nombre de escena → índice numérico |

**El campo `id` del CSV es directamente el número de bit.** Y encaja con lo medido en RAM sin tocar nada:

```
Mido's House Top Left/Right/Bottom L/R   chest  KOKIRI_MIDO    id 0x00-0x03
Kokiri Forest Kokiri Sword Chest         chest  KOKIRI_FOREST  id 0x00
OOT_KOKIRI_MIDO:   0x28   -> escena 40, chest = 0x0F   ✓
OOT_KOKIRI_FOREST: 0x55   -> escena 85, chest = 0x01   ✓
```

`mkchecks.py` cruza los tres ficheros y genera `checks.json` con la dirección y el bit de cada ubicación. `ootmm.py checks` lee el estado y lo resuelve a nombres, cruzando con el spoiler para mostrar qué item hay en cada uno:

```
COFRES DE OoT: 5 / 305  (1.6%)

  KOKIRI_MIDO  (escena 0x28)
    [x] bit  1  Mido's House Top Right     ->  Minuet of Forest
    [x] bit  3  Mido's House Bottom Right  ->  Blast Mask
  KOKIRI_FOREST  (escena 0x55)
    [x] bit  0  Kokiri Forest Kokiri Sword Chest  ->  Recovery Heart
```

### Dónde va cada tipo de check

`packages/generator/src/common/mark.c` (copia en `data/ref/`) tiene el switch completo:

```c
OV_CHEST        perm[scene].chests       |= 1 << id
OV_COLLECTIBLE  perm[scene].collectibles |= 1 << id
OV_GS           BITMAP32_SET(gsFlags, id - 8)
OV_COW          gCowFlags |= 1 << id
OV_NPC/SHOP/SCRUB/SR/FISH   BITMAP8_SET(gSharedCustomSave…, id)
default         xflags → BITMAP8_SET(gSharedCustomSave.oot.xflags, bitPos)
```

**`gSharedCustomSave` (con el juego en OoT): `0x8044B570`.** Se despejó de un solo dato medido: al comprar el `Kokiri Shop Item 2` (id 1) se encendió el bit 1 del byte `0x8044B88A`, que es `shops[0]`. Restando el layout de `OotCustomSave` y `XFLAGS_COUNT_OOT = 0x2FA` sale la base.

```
oot.xflags[0x2FA]  0x8044B570      oot.scrubs[8]  0x8044B892
oot.npc[32]        0x8044B86A      oot.sr[16]     0x8044B89A
oot.shops[8]       0x8044B88A  ← medido
```

### Los xflags: las tres tablas de la ROM

El `bitPos` de un xflag no está en el CSV, sale de tres tablas encadenadas que viven en la ROM (`xflags.c`):

```c
setupIndex = tablaEscenas[sceneId] + setupId
roomIndex  = tablaSetups[setupIndex] + roomId*12 + sliceId
bitPos     = tablaRooms[roomIndex] + actorId     // tablaRooms es s16
```

**Cómo se llega a las tablas.** `custom.h` de v32.0 da sus VROM; como son `>= 0x08000000`, `comboDmaLookup` las manda a la *extra DMA*: un `u32` en `COMBO_META_ROM = 0x03FFF000` da la dirección física de una tabla de `DmaEntry`, y el `u32` siguiente su número de entradas. Las seis salen sin comprimir (`pend == 0`), así que `pstart` es el offset directo dentro del `.z64`:

| Tabla | VROM | ROM (esta seed) | Entradas |
|---|---|---|---|
| OoT escenas | `0x80B0F00` | `0x3D2F280` | 101 |
| OoT setups | `0x80B0FD0` | `0x3D2F350` | 142 |
| OoT rooms | `0x80B10F0` | `0x3D2F470` | 6252 |
| MM escenas | `0x80B41D0` | `0x3D32550` | 114 |
| MM setups | `0x80B42C0` | `0x3D32640` | 118 |
| MM rooms | `0x80B43B0` | `0x3D32730` | 4524 |

**El `id` del CSV era una clave empaquetada, no un número de bit.** Lo genera `packages/generator/scripts/xsanity.ts`, que es también quien construye las tablas:

```
key = (sliceId << 16) | ((setupId & 3) << 14) | (roomId << 8) | actorId
```

Por eso los ids de las filas xflag tienen cinco dígitos hex y los de cofres cuatro. El `roomId` de 6 bits da sitio de sobra al truco de las grutas (`0x20 | grottoData`) que hace `comboXflagInit`.

**Vanilla y MQ comparten bit.** De los 2336 xflags de OoT, 149 pares colisionan en `bitPos`, y los 149 son exactamente un check vanilla contra su equivalente `MQ …`: las tablas no distinguen las dos versiones porque en una seed dada sólo existe una. Cero colisiones dentro del mismo grupo, y cero en MM (2415 de 2415 distintos). El spoiler dice cuál toca (`Master Quest Dungeons:`); esta seed es `none`. `mkchecks.py` marca esas filas con `"mq": true` y deja la decisión al consumidor.

**Verificado con el save.** De los 12 bits encendidos en `oot.xflags`, los 12 mapean a un check con nombre y todos caen en Kokiri Forest y la casa de Saria, que es justo donde va la partida. Ni un bit huérfano. Cruzado por dos vías independientes: el volcado de RAM y el `.fla`.

### El custom save de MM: estaba pegado detrás del de OoT

`MmCustomSave` va justo después de `OotCustomSave` dentro de `SharedCustomSave`. `OotCustomSave` acaba en `0x377` (`sr` acaba en `0x33A`, padding a `0x33C`, dos `OotRespawnData` de `0x1C`, `powderKegTimer` s16 en `0x374`, bitfield en `0x376`) y lleva `ALIGNED(16)`, así que **`MmCustomSave` empieza en `+0x380`**.

```
mm.xflags[0x350]  0x8044B8F0      mm.shops[4]    0x8044BC60
mm.npc[32]        0x8044BC40      mm.halfDays    0x8044BC64
```

Confirmado por partida doble contra el `.fla`: el bitfield de cola de `OotCustomSave` aparece exactamente en `+0x376`, y `mm.halfDays` vale `0x3F` exactamente en `+0x6F4`, que es donde este layout lo coloca. De propina, `mm.npc` byte 12 bit 0 encendido = `Initial Song of Healing`, que es el primer check de MM de la partida.

> Por qué falló la caza anterior: se buscaba el byte en la RAM, y `gSharedCustomSave` está en una dirección distinta según qué mitad del juego corre. Con OoT corriendo la base es `0x8044B570` y desde ahí se leen **también** los flags de MM, porque el bloque es compartido. Con MM corriendo la base es otra y sigue sin localizar.

**Tercera vía de lectura, sin emulador.** `save.c` guarda el bloque entero en flash: `Flash_ReadWrite(0x18000 + 0x4000 * fileIndex, &gSharedCustomSave, …)`. Los mismos offsets valen sobre un `.fla` desword-swapeado.

### El layout de escenas de MM

Sale entero de la cuenta de estructura, sin cazar nada. **Cuidado con la base de MM**: la que usa el proyecto (`0x8044BE18`) se fijó por la firma, con la convención de OoT de que `newf` está en `base+0x1C`. En `MmSave` el `newf` está en `+0x24`, así que esa base es en realidad `MmSave+0x08`. No se toca: todos los offsets de `inventory.py` son relativos a ella y están medidos en juego.

De ahí `info = base+0x1C`, y `MmSaveInfo` se recorre solo:

```
playerData   0x00  (0x28)      inventory  0x4C  (0x88)
itemEquips   0x28  (0x22)      perm       0xD4  <- permanentSceneFlags[120]
```

**`permanentSceneFlags` de MM = `base+0xF0` = `0x8044BF08`** con OoT corriendo. El orden de campos **no** es el de OoT: MM tiene dos `switch`, así que `collectible` cae en `+0x10` y no en `+0x0C`.

```c
chest 0x00   switch0 0x04   switch1 0x08   clearedRoom 0x0C
collectible 0x10   clearedFloors 0x14   rooms 0x18
```

**Estado de la verificación: derivado, no medido.** La tabla está entera a cero en los dos volcados, porque de MM sólo hay un check hecho (`Initial Song of Healing`) y ningún cofre abierto. Lo que sí está medido en juego son los seis offsets que produce exactamente la misma cuenta —`items 0x68`, `ammo 0x98`, `upgrades 0xB0`, `quest 0xB4`, `strayFairies 0xCC` y `skullCountSwamp 0xEB8`, este último ya **detrás** de la tabla—, y con esos seis anclados no queda holgura para colocar `perm` en otro sitio.

> **Predicción falsable, para la próxima sesión de juego.** El primer cofre que abras en Termina debe encender un bit en `0x8044BF08 + escena*0x1C`. Los de gruta (escena 0x07) van todos al u32 de `0x8044BFCC`: p. ej. `Termina Field Dodongo Grotto` → bit 0, `Deku Palace Grotto Chest` → bit 5. Recuerda que el flag no se escribe al abrir el cofre sino al salir de la escena.

### Las gold skulltulas

No son custom save, son un campo más de `OotSaveInfo`, así que salen de la misma cuenta de estructura:

```c
OV_GS   BITMAP32_SET(gOotSave.info.gsFlags, id - 8)
#define BITMAP32_SET(m,b)  ((m)[(b) >> 5] |= (1 << ((b) & 0x1f)))
```

**`gsFlags[6]` = `base+0xE9C` = `0x8011B46C`.** LSB primero dentro del u32, sin trampa. Los ids del CSV aquí **sí** son números de bit de verdad, a diferencia de los xflags; van en bloques de 8 por grupo de escena (el bloque 0 está reservado, de ahí el `−8`) y llegan a 179, o sea bits 0..171 de los 192 disponibles.

Las 144 filas son **100 vanilla + 44 Master Quest**, y los 44 MQ colisionan uno a uno con un vanilla, igual que en los xflags. Las 100 vanilla dan 100 pares `(addr, bit)` distintos.

El recorrido de la estructura **cierra por los dos lados**, que es lo que lo hace fiable sin medirlo: hacia delante `perm` acaba en `0xE64`, `fw` ocupa `0x28` → `0xE8C` (que es literalmente el nombre del campo siguiente, `unk_e8c`), `+0x10` → `0xE9C`; hacia atrás, `unk_EB4` fija el final de `gsFlags[6]` en `0xEB4 − 0x18 = 0xE9C`. Y siguiendo desde ahí se cae exactamente en `eventsMisc = 0xEF8`, que sí tiene `ASSERT_OFFSET`.

> **Predicción falsable, y cómo se resolvió.** Se predijo: si el jugador ha matado 3 skulltulas, deben salir 3 bits; si tiene 3 *tokens* pero no ha matado ninguna, cero bits, porque en OoTMM los tokens son items que caen de cualquier ubicación. Salió **cero bits con 3 tokens en el inventario**: la segunda rama. El cruce de los 88 checks completados contra el spoiler da exactamente `3 Gold Skulltula Token`, todos procedentes de otras ubicaciones. El offset no queda confirmado, pero tampoco falsado. Sigue pendiente de la primera skulltula que se mate: la de Kokiri Forest enciende un bit en `0x8011B478` (`GS Soil` → bit 0, `GS Night Child` → bit 1), las del Deku Tree van al `0x8011B46C`.

### El overlay, en vivo sobre la partida real

La sesión del 13 de agosto con el overlay corriendo en OBS dio **96 de 4.995**, y confirma con datos dos cosas que hasta ahora eran derivaciones:

- **Los xflags de MM se leen con OoT corriendo.** `TERMINA_FIELD 28/277`, `GROTTOS 26/450`, `SOUTHERN_SWAMP 12/45`, `MILK_ROAD`, `CLOCK_TOWN_SOUTH`… todo en su color, mientras el juego activo es Ocarina. Eso valida el **rebase del ancla `custom`** y de paso el `+0x380` del custom save de MM.
- La **medida de confianza** se mantuvo por encima del umbral toda la sesión, que es la comprobación continua de que las bases están donde creemos.

Siguen sin confirmar, y siguen esperando el mismo gesto: `gsFlags` hasta que se mate una gold skulltula de verdad (las de Kokiri Forest seguían en la lista de pendientes), y el `perm` de MM hasta que se abra un cofre en Termina.

### Validación en vivo: 88 checks sobre partida real

La lectura en vivo del 13 de agosto, con la partida ya en Termina, dio **88 checks completados de 5.963**, y es la mejor validación que tiene el proyecto:

- **Los 88 nombres existen los 88 en el spoiler.** Ni un huérfano, ni un nombre inventado.
- **Los items que suman cuadran con el inventario real**: Powder Keg, Blast Mask, Hover Boots, Bombchu Bag, Cojiro, Minuet of Forest, Bolero of Fire, Zelda's Lullaby, Progressive Sword, Deku Stick Upgrade…
- **Los xflags de MM disparan sobre progreso real** y agrupados donde el jugador ha estado: Termina Field, Southern Swamp, Milk Road, Clock Town South, la cow grotto de Termina Field. Si el mapeo estuviera mal saldrían bits sueltos por escenas donde nunca se ha entrado, que es justo lo que no pasa.
- **`Initial Song of Healing` sale marcado**, y eso es el custom save de MM (`mm.npc` byte 12 bit 0) leído en vivo desde la base derivada `+0x380`. Coincide con lo que ya se veía en el `.fla`. El `+0x380` deja de ser sólo una cuenta de estructura.
- **`perm` de MM sigue sin confirmar**: no hay ningún cofre de MM abierto todavía. El de OoT sí se confirma, con los cuatro de Mido's House y el Kokiri Sword Chest.

> Gotcha de presentación que salió de aquí: los ids de escena **se repiten entre juegos** (0x2D es `KOKIRI_SHOP` en OoT y `TERMINA_FIELD` en MM). Agrupar por `scene_id` sin el juego junta dos escenas distintas bajo la misma cabecera. `cmd_checks` ya agrupa por `(game, scene_id, scene)` y prefija `[OOT]` / `[MM]`.

### Estado del mapeo: 5963 de 6043

| Destino | Checks | Resueltos | Estado |
|---|---|---|---|
| `xflags` | 4751 | 4751 | ✅ tablas de la ROM, verificado contra el save |
| `scene` (chest + collectible) | 562 | 562 | ✅ OoT medido; MM derivado, pendiente de un cofre |
| `gs_flags` | 144 | 144 | ✅ `gsFlags[6]`; derivado, pendiente de una skulltula |
| `custom` (shop, scrub, sr, npc, fish) | 539 | 506 | faltan los 33 `fish` (`caughtFishFlags`) |
| `mm_stray_fairy` | 29 | 0 | falta localizar |
| `cow_flags` | 18 | 0 | `SAVE_EXTRA_RECORD(u32, 9)`, hay que resolver el macro |

`mkchecks.py --rom <seed.z64> --spoiler <spoiler.txt>` es lo que produce esta tabla.

### Lo que falta del sistema de checks

Quedan **80 checks, el 1,3%**, y los tres bloques son de los que cuestan un experimento en la partida real. No merece la pena ir a por ellos de frente: cógelos oportunistamente, con savestate, cuando pases por uno.

- **Confirmar en juego** el `perm` de MM y el `gsFlags` de OoT. No es trabajo, es mirar un byte cuando toque; las predicciones concretas están arriba.
- **`caughtFishFlags`** (33): está en `SharedCustomSave`, detrás de los dos custom saves y de los bloques de souls. Es una cuenta de estructura más, pero con bitfields por medio.
- **`cow_flags`** (18): `SAVE_EXTRA_RECORD(u32, 9)`, hay que ver qué es ese macro.
- **stray fairies de MM** (29).
- **P4 y P6**, la parte multi.
- Validar con una segunda seed. Hay tres cosas nuevas que validar: que las tablas de xflags estén en los mismos VROM (deberían: son constantes de `custom.h`, no dependen de la seed), que el `+0x380` del custom save de MM aguante, y el `+0xF0` de `perm`.

## El overlay

`python ootmm.py overlay` levanta tres cosas: un hilo que sondea la memoria por `tracker.lua`, un servidor HTTP en `127.0.0.1:8013`, y una ventana propia en modo app (`--app=` de Edge o Chrome, sin barras de navegador).

Para OBS hay dos caminos y los dos salen de ahí: capturar esa ventana, o apuntar un **Browser Source** al mismo URL, que es lo que conviene porque permite fondo transparente y escalado limpio.

### ROMs comprimidas, y por qué `checks.json` es de una versión concreta

Salió al arrancar el overlay con otra seed, una de mayo: `ValueError: la tabla 0x80b0f00 esta comprimida`. Dos suposiciones mías que no valen en general, las dos en el mismo sitio:

- **OoTMM puede generar la seed comprimida**, y entonces las entradas de la DMA llevan Yaz0.
- **Las seis tablas de xflags pueden compartir una única entrada** de la DMA, en vez de tener una cada una. Hay que quedarse con el trozo que empieza en el VROM pedido, no con el fichero entero.

Las dos viven ahora en `rom.py`, que usan `mkchecks.py` y `mkicons.py`.

**Pero arreglarlo destapó lo de debajo**: con esa ROM las tablas se leen y salen **3.414 de 4.751 xflags con bit imposible**, negativos incluso. Las VROM de `custom.h` son constantes **de v32.0**, así que con una seed de otra versión apuntan a datos que no son.

Y lo grave: `mkchecks` escribía el `checks.json` igualmente. Como esto lo dispara el overlay solo al arrancar, machacaba un fichero bueno con uno inservible sin que nadie se enterara. Ahora hay una barrera: si más del 2% de los xflags da un bit imposible, **aborta sin tocar nada** y dice por qué, y el overlay avisa de que los checks que va a usar son de otra ROM.

> Los **iconos sí valen entre versiones**: `icon_item_static` está en el mismo índice de la dmadata de OoT en todas, así que `mkicons.py` funciona con cualquier seed. Lo que está atado a v32.0 son las tablas de xflags.

### No hay que decirle nada: la ROM se detecta sola

`discover.py`. Antes había que pasar `--rom` y `--spoiler` a mano y acordarse de regenerar `checks.json` e `icons.json` al cambiar de seed. Ahora sale de lo que el emulador ya sabe.

**La clave está en el nombre de la carpeta de partidas.** Project64 guarda en `Save/OOT+MM COMBO-<hash>/`, y ese hash es el **MD5 de la ROM con las palabras de 4 bytes invertidas**, que es el orden en que el emulador la guarda por dentro. Medido, no supuesto: el MD5 del fichero tal cual da `5DFC2740…` y no casa con nada; el de la versión swap4 da `7EE74762…`, que es exactamente la carpeta de esta seed.

De ahí sale una cadena que no depende de adivinar:

1. la carpeta de save escrita más recientemente dice **qué seed estás jugando**
2. su hash identifica la ROM **sin ambigüedad**
3. se busca esa ROM entre las recientes de `Project64.cfg` (`Recent Rom N`)
4. el spoiler se coge de al lado de la ROM: para `OoTMM-<id>.z64` se prefiere `OoTMM-Spoiler-<id>.txt`

Si nada casa se cae a `Recent Rom 0`, que es la última que se abrió. `checks.json` e `icons.json` guardan de qué ROM salieron, así que se regeneran solos cuando cambias de seed y no se tocan cuando ya están al día. El hash se cachea por `(ruta, mtime, tamaño)` para no releer 64 MB en cada arranque.

Se puede saltar todo con `--rom` / `--spoiler` explícitos, o desactivarlo con `--no-auto`.

> Se miró antes si el Lua podía dar la ruta de la ROM directamente, que era lo primero que uno intenta. La API de P64-EM que usa `tracker.lua` sólo expone `memory`, `socket`, `binary` y `print`, y no hay documentación; la config del emulador resultó ser un camino mejor y, sobre todo, verificable sin tener el emulador abierto.

### Un URL por panel

`/` es la **vista de director**, con todo junto, para el monitor del que juega. Cada panel se sirve además suelto en `/p/<nombre>`, sin cromo y ocupando la fuente entera, para añadirlo como su propio Browser Source y colocarlo donde se quiera. Así el streamer enseña sólo lo que le interesa.

| Panel | URL |
|---|---|
| Resumen y cifras | `/p/summary` |
| Progreso por región | `/p/regions` |
| Rejilla de items | `/p/items` |
| Feed de actividad | `/p/activity` |
| Pendientes de la zona | `/p/remaining` |

> Los nombres viejos en español (`/p/resumen`, `/p/regiones`, `/p/actividad`,
> `/p/pendientes`) **siguen respondiendo**, con un 301 al nuevo y conservando
> el query. Es lo que apunta cualquier Browser Source montado antes del cambio
> de idioma, y renombrar un URL rompe una escena de OBS en silencio: la fuente
> se queda en blanco y no dice por qué.

Es una sola página: el servidor sirve el mismo `overlay.html` para `/` y para `/p/*`, y la página mira su propia ruta, borra del DOM los bloques que no son suyos y se queda con uno. La vista de director lleva un desplegable que genera los URLs de los paneles ya con las opciones puestas, y un botón de copiar por cada uno.

### Filtrar el relleno

`?junk=hide`, o el selector **Mostrar** de la vista de director. Deja el progreso por región y los pendientes con **sólo lo que importa**: de 4.995 checks a **612**, y la lista de pendientes de una zona baja de 73 a 2.

Dos cosas que había que resolver, las dos anotadas en su día en el `BACKLOG.md`:

- **El filtro va por el item que hay dentro, no por el tipo de ubicación.** Con el pool barajado, una mata de hierba puede llevar las Hover Boots; en esta partida `Kokiri Forest Rupee Child 2` daba un Swamp Skulltula Token.
- **Y por tanto necesita el spoiler**, lo que chocaba con `?spoiler=off`. Se resuelve **clasificando en el servidor**: la página recibe un `junk: true/false` por check y filtra con eso, sin ver nunca el nombre del item. Sin spoiler el filtro se desactiva solo en vez de dejarlo todo a cero.

La regla es por nombre, no por frecuencia, y eso fue una decisión medida: `Gold Skulltula Token` sale **100 veces** y no es relleno, ni las Stray Fairy. Contrastando la lista con el spoiler aparecieron además dos trampas:

- Las **rupias de puzle** van entre paréntesis —`Silver Rupee (Shadow Temple - Scythe)`— y **no son relleno**: pueden hacer falta para progresar. El patrón de rupias tuvo que pasar a ser exacto y sin sufijo.
- La **munición puede llevar el juego detrás** (`5 Arrows (OoT)`), y se colaba como importante.

### Los selectores aplican a lo que estás mirando

Los tres selectores de la vista de director —Mostrar, Spoiler, Fondo— sólo
llamaban a `renderUrls()`. Componían los enlaces de los paneles con la opción
puesta, pero **la vista de al lado seguía igual**: cambiabas a «sólo lo
importante» y los números no se movían. Eso no se lee como «este control es
para otra cosa», se lee como un filtro roto, y con razón.

Ahora aplican en el sitio. Tres cambios pequeños y uno que no era obvio:

- Las opciones eran `const` leídas de la URL una vez. Son **estado**, no
  constantes: la URL sólo dice cómo se arranca.
- **Pintar va aparte de sondear.** `tick()` hacía las dos cosas, así que la
  única forma de repintar era esperar al siguiente sondeo. Partido en `tick()`
  (busca y guarda en `lastState`) y `render(s)`, que es lo que llama
  `applyOpts()` para repintar al instante.
- La opción elegida se escribe en la barra de direcciones con
  `history.replaceState`, así que **recargar no la pierde** y la URL de arriba
  sirve igual que las de los paneles.

> **El que no era obvio: aplicar el fondo en vivo escondía los propios
> controles.** Existe `body.chroma-none .director { display: none }` —
> deliberado, porque `/?chroma=green` se usa para capturar la ventana entera y
> ahí los controles estorban. Pero al aplicarse en vivo, elegir
> «transparente» hacía desaparecer el selector con el que acabas de elegirlo, y
> no había vuelta atrás salvo editando la URL a mano. Se distingue por origen:
> si el fondo viene de la URL el director se esconde como siempre; si lo
> acabas de tocar tú, el `body` lleva `opts-live` y se queda.

De paso, la misma clase de trampa un escalón más abajo: **sin spoiler cargado
el filtro de relleno no puede funcionar** (`can_filter` del servidor) y se
desactivaba solo, en silencio. Ahora la opción sale deshabilitada y dice por
qué, «sólo lo importante (hace falta el spoiler)» — y hay un botón para
cargarlo sin reiniciar, que es la sección siguiente.

Verificado conduciendo Edge por CDP contra el overlay real servido sobre
`ram-en-oot.bin` —el `Tracker` de verdad, `checks.json` de verdad y el spoiler
de verdad, con un `link` que lee del volcado en vez de `tracker.lua`—:

| | sin filtrar | «sólo lo importante» |
|---|---|---|
| resumen | 18 / 4.995 | 4 / 612 |
| regiones con datos | 4 | 3 |
| pendientes de la zona | 25 (tope) | 2 |

`spoiler=full` hace aparecer el `→ item` en los pendientes y `off` lo quita,
las tres opciones sobreviven a una recarga, `/?chroma=green` sigue escondiendo
el director, los paneles sueltos no cambian y la consola sale limpia.

### Cargar el spoiler desde la página

El spoiler sólo se cargaba al arrancar, con `--spoiler` o detectado al lado de
la ROM. Si no aparecía, te quedabas sin filtro de relleno y sin saber qué hay
en lo pendiente **hasta reiniciar el overlay**, que en mitad de un directo no
es opción. Ahora hay un botón en la vista de director: `POST /spoiler` con el
contenido del fichero, y `Tracker.set_spoiler` recalcula en caliente lo único
que depende de él —la clasificación de relleno y los totales por región—.

**Se sube el contenido, no la ruta.** Un endpoint que abriera la ruta que le
pasen sería una lectura de fichero arbitraria, y el servidor puede acabar
escuchando fuera de `127.0.0.1` con `--http-host`.

Cargar el spoiler equivocado es peor que no cargar ninguno: los nombres casan
a medias y el filtro dice que sobran cosas que no sobran. Tres números lo
vigilan, y el tercero salió de probarlo:

| Comprobación | Qué corta |
|---|---|
| `Version:` de la cabecera contra la de `checks.json` | otra versión de OoTMM |
| cuántas de sus ubicaciones existen en `checks.json` | no es un spoiler, o es de otra versión |
| **cobertura**: cuántos de nuestros checks nombra | otra seed, o un spoiler que no da para clasificar |

> **La cobertura no estaba prevista y es la que importa.** El spoiler de otra
> seed v32.0 traía 980 ubicaciones y **las 980 casaban**, así que pasaba las
> dos primeras barreras tan campante. Pero cubría 934 de 4.995 checks, y de lo
> que no nombra `is_junk` no puede decir nada: el filtro se quedaba sin
> clasificar y «sólo lo importante» seguía enseñando los 4.995 — un filtro que
> no filtra, que es exactamente el fallo que se acababa de arreglar arriba.
> No se rechaza, porque una seed con otros ajustes tiene de verdad menos
> ubicaciones, pero se avisa. El bueno cubre 4.939 de 4.995.

Medido con el overlay servido sobre el volcado, subiendo cuatro ficheros por
CDP (`DOM.setFileInputFiles`):

| Fichero | Resultado |
|---|---|
| spoiler v30.1 | rechazado, «es de v30.1 y checks.json de v32.0» |
| `README.md` | rechazado, «ahí no hay ubicaciones» |
| vacío | rechazado |
| otra seed v32.0 | aceptado **con aviso**: cubre 934 / 4.995 |
| el de la seed | aceptado: 5.018 ubicaciones, cubre 4.939 / 4.995 |

Y detrás, lo que se buscaba: con el bueno cargado, «sólo lo importante» pasa
de 18 / 4.995 a **4 / 612** y los pendientes de la zona de 25 a 2, sin
reiniciar nada. Un rechazo no toca el estado: la opción sigue deshabilitada.

**Se aplica solo, porque cargarlo y que no cambie nada es la misma trampa.**
El spoiler es justo lo que hace posible filtrar el relleno y decir qué hay en
cada pendiente, así que al cargarlo se encienden las dos cosas —`junk=hide` y
`spoiler=full`— y los selectores se mueven a la vista, que es el acuse de
recibo. La casilla **aplicarlo a los paneles** lo desactiva para quien
prefiera cargarlo sin que se le mueva nada.

Medido, con la partida recién empezada del volcado:

| | antes | al cargar |
|---|---|---|
| resumen | 18 / 4.995 | 4 / 612 |
| pendientes en Kokiri Forest | 25 (tope) | 2 |
| primer pendiente | `GS Soil`, sin item | `Grass Adult 07 → Goron Lullaby` |
| regiones con datos | 4 | 3 |

Con la casilla sin marcar, los cuatro números se quedan como estaban y sólo
cambia el mensaje.

> **Y un cuidado que salió de aquí.** `spoiler=full` entra también en los
> enlaces de los paneles, y esos van a OBS: enseñar lo pendiente es opción
> legítima en el monitor del que juega, pero en una fuente de captura lo ve el
> público. La vista de director avisa cuando el nivel es `full`.

### Un juego por overlay

`?game=oot` o `?game=mm` deja el overlay con un solo juego, para montar el tracker de Ocarina y el de Majora por separado. No es sólo un filtro visual de la rejilla: afecta a todo.

- El **porcentaje del resumen** pasa a ser el de ese juego, no el de los dos juntos — hay totales por juego en el estado (`totals`, `done_by_game`).
- La **chapa** identifica el overlay en vez del juego activo, y el medidor toma el color de ese juego.
- El **feed** se filtra por juego (cada entrada lleva el suyo).
- Los **pendientes** son los de la zona donde estás, así que un overlay filtrado por el otro juego dice «ahora mismo estás en …» en vez de quedarse mudo.
- Con filtro se quita la cabecera repetida del juego dentro del panel, porque el título ya lo dice.

La vista de director genera también estos URLs: `/p/items?game=oot`, `/p/items?game=mm`, y lo mismo para regiones y resumen.

### Los iconos salen de la ROM

`mkicons.py --rom <seed.z64>` produce `icons.png` y `icons.json`. Nada viene de fuera: los iconos son los del juego.

- **`icon_item_static`** es el fichero 8 de la `dmadata` de OoT (en `0x7430`, según `combo/dma.h`), sin comprimir, con iconos de 32×32 RGBA32. **El índice del icono es el id del item**, así que `items.h` los nombra todos y no hay que contar posiciones a ojo.
- El tramo válido se **midió**, no se supuso: se marca válido el icono con las cuatro esquinas transparentes y entre 80 y 1000 píxeles opacos, y sale un tramo limpio `0x00..0x58` seguido de ruido.
- Medallones y piedras (`0x66..0x79`) no están ahí sino en **`icon_item_24_static`** (fichero 9), a 24×24, donde el índice es `id − 0x66`. Se centran en una celda de 32.

Para los huecos `item:` no hace falta tabla: el valor leído **ya es el id**, o sea el índice de la hoja. Sí hace falta para lo que es booleano o nivel, y para saber qué icono enseñar **apagado**: un hueco vacío vale `0xFF` y no dice qué item le tocaba, así que sin eso todo lo que aún no tienes saldría como texto, que es lo contrario de lo que sirve una rejilla.

### Los iconos de MM: un archivo CmpDma

Aquí me equivoqué a lo grande y conviene dejar escrito el porqué, que es más útil que el resultado.

Di por hecho que no estaban porque busqué **imágenes** y lo que hay son **datos comprimidos**. Los ficheros 8 y 9 de MM (`icon_item_static`, `icon_item_24_static`) sí están marcados como ausentes en la dmadata, y eso lo tomé como prueba. Pero MM no los carga por la dmadata: usa `CmpDma_LoadFile`, y el arte vive en un **archivo CmpDma**, una tabla de offsets seguida de los ficheros, **cada uno comprimido por separado con Yaz0**. En crudo eso no se parece a un icono en ningún formato, así que ningún barrido de píxeles —RGBA32, RGBA16, CI4, CI8, ventana deslizante— podía encontrarlo.

Lo que lo resolvió no fue seguir escaneando, sino **mirar la documentación**: el decomp de MM (`zeldaret/mm`) nombra el asset (`icon_item_static_yar`), el mecanismo (`sys_cmpdma.c`) y el formato de dibujo (`G_IM_FMT_RGBA, G_IM_SIZ_32b`).

El formato, de `src/code/sys_cmpdma.c`:

```
u32 dataStart      tamaño de la tabla; hay dataStart/4 - 1 ficheros
u32 offs[...]      inicio de cada fichero, relativo a seg + dataStart
                   (el fichero 0 empieza en 0; su fin es offs[1])
```

Y `CmpDma_LoadFile(segmento, id, ...)` usa **el id del item como índice**, así que la entrada `i` es el icono de `ITEM_MM_* == i`. Verificado: la 0x32 es la Deku Mask, y de la 0x32 a la 0x49 salen las 24 máscaras en orden.

El archivo no está en un sitio fijo, así que se localiza **por su forma**: se parte de cada bloque `Yaz0` de la ROM y se prueba si `dataStart` bytes antes hay una cabecera que apunte justo ahí. De los siete archivos CmpDma que aparecen se coge el que más entradas tiene de 4096 bytes exactos, que es el de los iconos: **98**.

Está en la ROM combinada igual que en la base, así que sale de la seed de cada uno.

**La lección**: «no lo encuentro» no es «no está», y cuando algo tiene que existir por fuerza —el juego lo dibuja— lo que falla es el método de búsqueda. Antes de la quinta pasada de heurísticas, leer la documentación del formato.

**Dónde deja de valer el índice.** El archivo va empaquetado y **las doce canciones no tienen entrada**: el juego las dibuja todas con una misma textura de nota que vive en `code`, no en el archivo (`gItemIconSongNoteTex`). Como faltan, todo lo que viene después queda desplazado — la entrada `0x61` es el cuaderno de los Bombers, no la Sonata. Por eso el mapa se corta en `0x60` (`ITEM_REMAINS_TWINMOLD`), que es el último que cuadra. Se descubrió al intentar usar la nota: salían manchas de color en vez de notas.

Con eso, de la ROM salen las 24 máscaras, los cuatro restos de jefe y el resto de items de Termina. Las notas de canción siguen dibujadas, con los colores del juego, y con la forma de la del menú: **corchea simple** —cabeza inclinada abajo a la izquierda, plica a la derecha y banderola—, no la corchea doble con barra que había al principio.

### Lo que hay en cada panel, y por qué

- **Equipo de MM.** MM no tiene fuerza ni escama: esos campos están en la estructura porque `MmUpgrades` copia la de OoT, pero el juego no los usa. En su sitio va la progresión de **espada** (Kokiri, Razor, Gilded) y **escudo** (Héroe, Espejo), que salen de los nibbles de `MmItemEquips` en `base+0x64`. Verificado en los dos volcados: `0x0010` en ambos, o sea escudo 1 y ninguna espada, que es como empieza una seed.
- **Los huecos vacíos de MM ya salen en gris.** El orden de huecos sale del decomp (`z64item.h`) y coincide con los ids de item: el hueco 0 es la ocarina, el 1 el arco, el 8 los palos. Los seis últimos son botellas y todos enseñan la vacía.
- **Fuera las mejoras de palos y nueces**, en los dos juegos: ocupaban celda y no dicen gran cosa.
- **La Nana de Zelda lleva una trifuerza** en vez de una nota, que la distingue de las demás de un vistazo.
- **Pluma para la canción del vuelo**: la flecha hacia arriba no se leía.
- **El progreso de MM va reordenado.** Sale en orden de bit, que mezcla restos y canciones y deja el cuaderno y la media de los Goron sueltos al final. Puestos los cuatro restos, el cuaderno y la media en la primera fila, las doce canciones ocupan **dos filas enteras** y en el orden canónico del juego, el de sus ids: Sonata, Nana Goron, Bossa Nova, Elegía, Juramento, Saria · Tiempo, Curación, Epona, Vuelo, Tormentas, Sol.

De paso quedó escrito un descompresor **Yaz0** en 30 líneas, que hizo falta porque la mitad de MM viene comprimida.

### Lo que la ROM no trae, se dibuja

Las canciones no son items y no tienen icono en ninguna parte de la ROM, así que se dibujan como SVG en un lienzo de 24×24, escalando con la celda.

**Las seis canciones de teletransporte llevan su color, y el color es el dato**: verde bosque, rojo fuego, azul agua, naranja espíritu, morado sombras, amarillo luz. Son los del juego, no elegidos por gusto. Las otras seis el juego las pinta en blanco, así que ahí lo que distingue es el símbolo: rayo para las tormentas, herradura para Epona, hoja para Saria, sol, reloj de arena para el tiempo. En MM se añaden corazón (curación), flecha arriba (vuelo), onda (New Wave), cuaderno y una calavera para los restos de cada jefe, esta con el color del jefe.

> **Las máscaras de MM sí están en la ROM, y se extraen.** Lo di por imposible tras varios barridos y me equivoqué: el usuario insistió con el argumento correcto —el juego las dibuja en su menú, luego el arte está ahí— y tenía razón. Ver la sección de abajo. Las siluetas dibujadas siguen en el código como respaldo, por si un día el archivo no se encuentra.

### Iniciales que se distinguen

Con etiquetas de dos letras chocaban: `RG` para «Restos de Goht» y «Restos de Gyorg», cuatro `SS` en las canciones de MM, tres `GM` entre las máscaras.

La regla no es alargar la última palabra —en las máscaras siempre es «Mask», y alargarla da `KafMas` / `KamMas`, largo e ilegible—, sino **quitar lo que comparten todas las que chocan** y desambiguar con lo que queda: `Kaf`, `Kam`, `Gib`, `Gar`, `Gia`. Tope de cuatro caracteres, y el tamaño de letra baja con la longitud para que no se salga de la celda.

### Imágenes puestas a mano

Carpeta `icons/`, con su `LEEME.md`. Lo que dejes ahí manda sobre el icono de la ROM, y está pensado para tapar lo que la ROM no tiene — las máscaras propias de MM. **No se descarga nada**: las imágenes las pone el usuario.

El nombre del fichero se compara normalizado, así que valen tanto el nombre que enseña el overlay (`deku-mask.png`) como el de `items.h` (`mask-deku.png`), y `icons/mm/` sólo aplica a Majora mientras que `icons/` vale para los dos.

Dos detalles que salieron al probarlo, los dos con el mismo tipo de causa —la ruta y el nombre no son lo mismo escritos que en memoria:

- **El apóstrofe se quita, no separa.** `Garo's Mask` normalizaba a `garo-s-mask`, así que el fichero `garos-mask.png` —que es como lo escribiría cualquiera— no casaba.
- **La URL hay que descodificarla.** Un fichero con espacios llega como `%20` y no casaba con lo que el escaneo había guardado.

El servidor sólo sirve ficheros que estaban al escanear la carpeta, comparando la ruta exacta contra esa lista, así que no se puede pedir nada de fuera por URL: comprobado que `/usericon/../overlay.py` da 404.

### La rejilla tiene la forma del menú del juego

No hizo falta buscar un layout por ahí: **ya está en los datos que leemos**.

| Rejilla | Columnas | De dónde sale |
|---|---|---|
| Items | 6 | `items[24]` y la pantalla del juego es de 6 columnas: el array **es** la rejilla, hueco por hueco |
| Máscaras (MM) | 6 | MM guarda 48 huecos: los 24 primeros son items y los 24 siguientes máscaras, otra página de 6×4 |
| Equipo | 3 | los nibbles de `OotEquipment` son espadas, escudos, túnicas y botas, tres de cada |
| Progreso | 6 | con 6 columnas los 24 bits de OoT caen solos en sus filas: medallones, canciones de teletransporte, canciones de ocarina, y piedras con lo demás |

La consecuencia es que **las filas significan algo**: una fila de espadas, una de escudos, una de medallones. Y los huecos vacíos se quedan puestos, porque son parte del dibujo del menú y no ruido — un hueco sin nombrar y vacío se pinta como casilla libre, sin etiqueta.

### La cifra de la esquina

`ammo[]` está indexado por hueco de inventario igual que `items[]`, así que el número cae en la misma casilla en la que el juego lo pinta: 10 palos deku, 20 nueces, 40 flechas. En las mejoras la cifra es el nivel. En un booleano no se pone nada — un «1» encima del icono sólo estorba.

> **El bug que destapó pedirlo.** En un hueco de inventario el vacío es `0xFF`, y el **0 es un id de item legítimo**: Deku Stick en OoT, Ocarina of Time en MM. La comprobación de «lo tengo» trataba el 0 como vacío, que es lo natural en todos los demás campos, así que **el primer hueco de las dos rejillas salía apagado para siempre**. Se veía como una casilla gris más entre veinticuatro, y sólo apareció al cruzar el conteo de munición con los datos crudos.

### El puente de nombres a iconos de MM

Emparejar por nombre exacto se queda corto: los dos juegos ordenan las palabras distinto (`MASK_KEATON` contra `KEATON_MASK`) y meten enlaces (`OCARINA_OF_TIME` contra `OCARINA_TIME`). Comparando el **conjunto de palabras** sin las de enlace, el puente pasa de 59 a 74 ids: entran las máscaras Keaton, Goron, Zora y Truth, la Ocarina del Tiempo, el Lente y el Escudo del Héroe.

Lo que **no** se hace es parecido difuso. Emparejaría `BOMBS_10` con `BOMBCHU_10` y `LENS_OF_TRUTH` con `MASK_OF_TRUTH`, que son items distintos; lo que no cae por la regla de palabras va en una tabla de alias explícita.

### La rejilla no scrollea

Una barra de scroll en una fuente de OBS es un defecto visible, y además mueve la composición según avanza la partida. Pero tampoco vale sólo con que quepa: **los grupos se colocan uno al lado de otro cuando hay ancho**. Apilados en una sola columna el alto manda, las celdas quedan diminutas y media fuente se queda vacía — que fue justo lo que se vio al montarlo en OBS de verdad. Por debajo de 420 px de ancho se vuelven a apilar, porque en una columna estrecha ponerlos en paralelo da un churro alto y fino.

Con eso, la celda **se busca lo más grande posible** en tres escalones del más fiel al que siempre entra:

1. layout del juego, con las columnas fijas, probando de 52 px hacia abajo
2. lo mismo en compacto: las cabeceras, que en una columna estrecha pesan más que las celdas, se reducen
3. flujo libre: se pierde la forma del menú pero entra en una columna estrecha

Con columnas fijas también puede desbordar **a lo ancho**, no sólo a lo alto, así que la comprobación mira las dos. El icono escala con la celda vía `background-size`, sin tamaños fijos.

Medido: un panel dedicado de 400×600 cabe exacto (`scrollHeight 440 = clientHeight 440`). La **vista completa** con los dos juegos no cabe ni en el tercer escalón y scrollea; es aceptable porque es la superficie de control, no una fuente de captura — para capturar están los paneles sueltos.

### Niveles de spoiler

**Lo que ya has cogido no es un spoiler** —quien mira te ha visto cogerlo—, pero lo que hay en una ubicación sin abrir sí lo es. Por eso el nivel por defecto enseña lo conseguido y calla lo pendiente.

| `?spoiler=` | Feed | Pendientes |
|---|---|---|
| `off` | sólo el nombre del check | sólo el nombre |
| `item` *(por defecto)* | item conseguido | sólo el nombre |
| `full` | item conseguido | item que hay dentro |

| `?chroma=` | Para qué |
|---|---|
| `none` | fondo transparente, para Browser Source |
| `green` | croma verde `#00b140`, para captura de ventana |

**Transparente quiere decir transparente.** Durante un tiempo `chroma=none` dejaba el `body` transparente pero las tarjetas al **82% de opacidad**, puesto por legibilidad: el resultado no era transparente, era un panel oscuro. Ahora la tarjeta no pinta nada, el texto se apoya en una sombra para leerse sobre cualquier escena, y queda un velo mínimo detrás de las rejillas para que los iconos apagados no se pierdan sobre fondos claros.

Medido de dos formas: los estilos calculados dan `rgba(0, 0, 0, 0)` en `body` y en `.card`, y una captura con alfa sale **94% transparente**, 5% translúcido y 1% opaco.

> **Sólo se ve transparente en OBS.** En una ventana de navegador —incluida la que abre `ootmm.py overlay`— siempre habrá un fondo oscuro detrás: el lienzo del navegador, que con `color-scheme: dark` es negro. La transparencia sólo la compone el Browser Source. Esto despistó también al comprobarlo: las capturas headless salían oscuras aunque la página fuese transparente.

### (resuelto) La escena que leemos ahora es donde estás

**Hecho el 14 ago 2026.** El panel de pendientes salía del **save context**, y
eso no es donde está el jugador:

```
OoT:  info.sceneId              ASSERT_OFFSET(OotSave, info.sceneId, 0x66)
MM:   playerData.savedSceneNum  info+0x26 -> base+0x42
```

**El defecto era de los dos juegos, no sólo de MM.** Se midió contra los dos
volcados, y el de OoT —que el backlog daba por «mejor»— también fallaba:

| Volcado | PlayState | Save context | |
|---|---|---|---|
| OoT | `0x2D` KOKIRI_SHOP | `0x55` KOKIRI_FOREST | una escena por detrás |
| MM | `0x6F` CLOCK_TOWN_SOUTH | `0x08` | ni siquiera la anterior |

O sea, el jugador estaba dentro de la tienda Kokiri y el overlay le enseñaba
lo pendiente del bosque.

**Dónde está el dato vivo.** OoTMM guarda `PlayState* gPlay` (`combo.h:186`),
así que la estructura es la del decomp y sus offsets salen de las cabeceras
del propio repo, sin cazar nada:

```
GameState (0xA4, combo/game_state.h)
  +0x00 gfxCtx*  +0x04 main  +0x08 destroy  +0x0C nextGameStateInit
  +0x10 nextGameStateSize  +0x14 input[4]  +0x74 tha  +0x84 unk[0x17]
  +0x9B running(u8)  +0x9C frameCount
PlayState (combo/{oot,mm}/play.h)
  +0xA4 sceneId u16     +0xB0 sceneSegment*
  roomCtx.curRoom.num s8:  OoT +0x11CBC   MM +0x186E0
```

> **La trampa: `running` está en `+0x9B`, no en `+0x98`.** `tha` acaba en
> `0x84` y `unk_84` mide `0x17`, que lo deja en dirección impar. Con el offset
> redondeado el barrido **no encuentra absolutamente nada**, y no hay ningún
> otro síntoma que lo delate: parece que el PlayState no está.

**No hizo falta localizar `gPlay`.** El game state se reserva una vez por
arranque y cae siempre en el mismo sitio, que resulta ser el que llevan usando
las herramientas de práctica de los dos juegos desde siempre:

```
OoT  0x801C84A0      MM  0x803E6B20
```

Los dos volcados los confirman, y con una prueba de propina que vale como
firma: **`main` apunta dentro del payload de OoTMM** —`0x80430A90` en OoT,
`0x80750488` en MM, contra `PAYLOAD_RAM` `0x80400000` y `0x80720000` de
`combo/defs.h`—, o sea que es el `Play_Main` parcheado. No es una dirección
cualquiera que se le parezca: es la de esta build.

**El embudo del barrido de respaldo**, por si la dirección conocida deja de
valer. Ocho filtros que no cuestan nada, y sobre los dos volcados dejan
**exactamente un candidato** cada uno:

| Filtro | OoT | MM |
|---|---|---|
| tres punteros a RDRAM | 10232 | 11058 |
| y distintos entre sí | 5511 | 6179 |
| `nextGameStateInit` y `Size` a cero | 148 | 132 |
| `running == 1` | 4 | 3 |
| `frameCount` < 2²⁸ | 2 | 3 |
| `sceneId` plausible | 1 | 2 |
| `sceneSegment` es puntero | 1 | 1 |
| `curRoom.num` entre −1 y 30 | **1** | **1** |

El de «distintos entre sí» es el que quita los buffers viejos llenos de un
mismo puntero repetido, que pasan todo lo demás sin despeinarse.

**El barrido está con cuenta atrás, y esto no es un detalle.** Lee los 8 MB
de RDRAM, y sin freno se dispararía en cada sondeo mientras estés en el title
screen o cambiando de escena — que es exactamente el problema que ya tuvo
`locate_saves`. `PLAY_RESCAN_SECONDS` lo limita a uno cada diez segundos. En
la práctica corre **cero veces**: la dirección conocida acierta a la primera y
el sondeo cuesta 12 lecturas, 0,02 MB.

Si no hay PlayState —title screen, arranque— se cae al save context de antes,
y el estado lleva `live: false` para que se sepa cuál de los dos está en uso.

### La distancia al custom save es de la versión, no del proyecto

**Hecho el 14 ago 2026**, del volcado de la seed experimental `dev-542a121`.
Síntoma: la progresión por región y la actividad vacías, y lo pendiente sin
marcarse al cogerlo, mientras el panel de pendientes iba bien.

La causa, medida: **en la dev todo el bloque se ha movido**.

| | buffer de MM | `gSharedCustomSave` | distancia |
|---|---|---|---|
| v32.0 | `0x8044BE18` | `0x8044B570` | `0x8A8` |
| dev-542a121 | `0x8044CF78` | `0x8044C6A0` | **`0x8D8`** |

Y con MM corriendo, medido después sobre `ram-dev-mm.bin`:

| | buffer de OoT | `gSharedCustomSave` | distancia |
|---|---|---|---|
| v32.0 | `0x8076C4F0` | `0x8076BC50` | `0x8A0` |
| dev-542a121 | `0x8076D400` | `0x8076CB30` | **`0x8D0`** |

Los dos lados crecieron **exactamente `0x30`**, que es la predicción que salía
de que el bloque engordara por la cola, y cierra el asunto: con la constante
vieja ese volcado daba confianza **0.167** y 4 checks de basura; con `0x8D0`,
**1.000** y los 18 reales.

La base de MM subió `0x1160` —que `locate_saves` absorbe sola, porque va por
firma— pero la distancia al custom save creció `0x30`, y esa **era una
constante**. Con ella el ancla caía en `0x8044C6D0`, `0x30` por delante:
confianza **0.077**, por debajo del umbral, así que el ancla `custom` entera se
descartaba y con ella **4.751 xflags y 506 bitmaps**. Exactamente el síntoma.

> Lo que hay entre medias es el save del otro juego, así que la distancia mide
> el tamaño de una estructura del generador. No hay razón para que no cambie:
> lo raro es que aguantara.

Ahora se **mide**: si las distancias conocidas no validan, se barre una
ventana de `0x800`–`0x1000` hacia atrás desde el buffer del juego inactivo. Una
sola lectura por juego cubre la ventana entera y los candidatos se puntúan en
local —leer cada uno por el enlace sería un megabyte por sondeo—, y el
resultado se cachea como distancia, así que el barrido corre una vez por
sesión. Con las seeds de v32.0 no corre nunca: la constante acierta.

**Tres cosas hicieron falta para que eligiera bien, y las tres fallaron primero.**

1. **`bits > 0`, no `bits >= 0`.** Una dirección que cae en ceros da confianza
   1.0 por vacuidad. Con `mejor_bits` empezando en −1, un candidato con **cero
   bits** validaba, ganaba, y el barrido no llegaba a correr nunca. Es la misma
   trampa que ya está documentada más arriba, mordiendo por tercera vez.
2. **Confianza primero, bits para desempatar.** Ordenando por número de bits,
   ganó `0x8044C754` —14 bits con confianza 0.929— frente a la buena, 7 bits
   con 1.000. El overlay pasó a reportar progreso en **Stone Tower y Spirit
   Temple en una partida que no había salido de Link's House**. La confianza es
   la que dice «esto es lo que creo que es»; los bits sólo desempatan entre
   direcciones igual de creíbles.
3. **Alineación a 16.** `gSharedCustomSave` es un global con `ALIGNED(16)`, y
   las tres bases medidas lo cumplen (`…B570`, `…BC50`, `…C6A0`). Barriendo de
   4 en 4 ganaba `0x8044C6B4`, que **también daba confianza 1.0 con los mismos
   7 bits** — pero mapeados a Lair Gohma y Zora River en vez de Link's House y
   Kokiri Forest. Con pocos bits encendidos la confianza sola no basta;
   alinear quita tres cuartas partes de los candidatos y deja la buena primera,
   1.000 contra 0.857 de la siguiente.

**Dos fallos más, que aparecieron al cruzar a Majora con la seed de la dev.**

> **Antes, una falsa alarma que conviene no repetir.** Al ver
> `MOUNTAIN_VILLAGE_WINTER 6/25` en una partida recién llegada a Termina lo di
> por basura, razonando con la progresión de MM vanilla: esa zona está pasado
> Snowhead. **Era correcto.** El jugador había cogido el item
> `Owl Statue (Mountain Village)` en la hierba de Kokiri Forest, se
> teletransportó, activó la estatua y rompió cinco bolas de nieve — y eso son
> exactamente los seis checks. En un randomizer con los búhos en el pool no hay
> zona tardía. **Los datos coherentes no son sospechosos por ser inesperados**;
> lo que hay que mirar es la escena viva y el item que da cada check, que aquí
> lo decían todo.
>
> Los dos fallos de abajo son reales y salieron de mirar eso a fondo, pero el
> síntoma que los destapó no era un síntoma.

- **Las constantes no pueden ganar por ser suficientemente buenas.** Aceptar el
  primer candidato que pasara el umbral significaba que, en una versión que
  mueve la estructura, una dirección desviada podía valer —sus pocos bits
  siguen cayendo en *algún* check conocido— y **la búsqueda que habría
  encontrado la buena no llegaba a correr**. Ahora se mide siempre, y la
  constante sólo ordena por dónde empezar a mirar. En v32.0 el barrido devuelve
  exactamente `0x8A8` y `0x8A0`, así que las valida en vez de suponerlas.
- **La distancia se cachea por el juego INACTIVO, que es del que cuelga.** La
  medida mientras MM estaba parado no dice nada cuando MM es el que corre: su
  buffer se ha ido a otro sitio. Reutilizarla al cruzar ponía el ancla en una
  dirección sin ningún sentido. Y el barrido sólo mira el lado del juego
  inactivo, porque una dirección colgando del que corre sólo puede ganar por
  casualidad.

Cruzar de juego además rearma la cuenta atrás del barrido: es un motivo
legítimo para volver a mirar, y sin eso el overlay leería el ancla equivocada
durante diez segundos justo después del cambio.

**Y el agujero de fondo, que era el de verdad.** Todo esto se rompió en
silencio, y eso es más grave que romperse. La segunda señal de la medida de
confianza —«había bits y ahora no hay»— **necesita haber visto bits antes en la
sesión** (`_xflag_peak > 8`), así que arrancando el overlay con la base ya mal
nunca llegaba a armarse: el overlay enseñaba una partida vacía, tranquila y
marcada como fiable. Ahora hay una segunda entrada a la misma alarma:

> **Hay checks de escena hechos y ni un solo xflag.** Los de escena cuelgan de
> otra ancla, localizada por firma, así que son de fiar **desde el primer
> sondeo**, que es justo cuando la otra señal no puede saltar.

Con un guardia para no gritar en falso: una seed generada **sin xsanity** no
tiene xflags, y ahí «cero xflags» no es una anomalía, es la verdad. Se
distingue porque esos checks sólo llevan `item` si están en la tabla de
colocación de la ROM; sin xsanity no lo llevan, y la alarma se desactiva sola.
Y el umbral es de 3 checks de escena, no 1, para que un solo cofre en una
partida nueva no la dispare.

Medido sobre `ram-en-mm.bin`, forzando la base a una zona de ceros:

| Escenario | confianza | ¿avisa? |
|---|---|---|
| base mala + progreso de escena + xsanity | **0.000** | **sí** |
| lo mismo, pero seed sin xsanity | 1.000 | no, y así debe ser |
| todo bien, con progreso | 1.000 | no |

Antes del cambio, el primer caso daba confianza 1.0 y `trusted: true`.

**La comprobación que lo cierra**, y son dos vías independientes. Localizando
por el patrón de bits del `.fla` de esa partida sale `0x8044C6A0` como única
dirección con confianza 1.000; y con el overlay leyendo ahí, las regiones que
salen son `LINK_HOUSE 1/1`, `KOKIRI_FOREST 5/85`, `HYRULE_FIELD 1/177` — los
mismos 7 checks que el `.fla`, escena por escena.

> Y de paso quedó descartado lo que parecía la causa obvia: **las tablas de
> xflags de la dev son idénticas a las de v32.0**, 0 diferencias en los 4.751
> `bitpos` y en todas las direcciones. El problema nunca estuvo ahí.

### Dos menús, no uno

Los ajustes vivían **dentro** del plegable «Capture in OBS», y se leían como
una nota al pie de una guía de captura. Son cosas distintas: las opciones se
tocan mientras juegas, los URLs de OBS se montan una vez y no se vuelven a
abrir. Ahora son dos:

| | |
|---|---|
| **Display options** | abierto por defecto: spoiler, mostrar, fondo, cargar spoiler |
| **Capture in OBS** | plegado: la explicación y la tabla de URLs |

Tres decisiones que no son de colocar cajas:

- **El interruptor de spoilers no está en ninguno de los dos.** Es el que
  buscas con prisa, en directo, y detrás de un plegable no hace su trabajo —lo
  decía ya un comentario del código y estaba dentro de uno—. Va suelto arriba,
  siempre a la vista.
- **El aviso de `spoiler=full` se muda al menú de opciones**, que es donde se
  toma la decisión. Estaba junto a la tabla de URLs («los enlaces de abajo
  revelan…»), y con el menú de OBS plegado no lo veía nadie.
- **Las reglas de ocultar pasan a un envoltorio `.dirtools`.** Nombraban
  `.director`, y partido en dos habrían tenido que enumerar cada pieza — con la
  garantía de que la siguiente se olvidaría. Es lo que mantiene funcionando el
  caso peliagudo de más arriba: elegir «transparente» en vivo no puede esconder
  el control con el que lo acabas de elegir. Verificado que sigue siendo así.

### El parpadeo de «unknown area», y el latch que faltaba

Reportado el 14 ago cruzando Termina Field: el título del panel iba y venía
entre `Remaining in TERMINA_FIELD` y `Remaining here / unknown area`.

Reproducido y explicado. **Una transición de escena reescribe el `PlayState`**,
así que durante uno o dos sondeos deja de validar —`running` a 0, punteros a
medio poner—. Y el código caía entonces al save context, que en MM da
`savedSceneNum`: en el volcado vale `0x08`, una escena que no está en la tabla,
así que `scene_names` devuelve `None` y el panel escribe «unknown area». De ahí
el parpadeo, y de ahí que sólo se notara en zonas con muchas transiciones.

El arreglo es el **latch** que el POC lleva recomendando desde el spike: si la
lectura viva falla, se conserva la última buena en vez de saltar a un dato
peor. El campo `live` del estado sigue diciendo cuál de las dos es.

```
sondeo normal                     escena=MOUNTAIN_VILLAGE_WINTER  live=True
PlayState no valida (transicion)  escena=MOUNTAIN_VILLAGE_WINTER  live=False
y otro sondeo igual               escena=MOUNTAIN_VILLAGE_WINTER  live=False
vuelve a validar                  escena=MOUNTAIN_VILLAGE_WINTER  live=True
```

> La lección general: **el respaldo tiene que ser mejor que nada, no peor que
> lo que ya tenías**. Caer del dato vivo al dato rancio parecía prudente y
> resultó ser la fuente del defecto.

### Las bases se eligen por parejas

Salió buscando por qué el resumen enseñaba rupias y corazones que no eran los
de la partida. **No está confirmado que fuera esto** —hace falta un volcado en
el momento— pero al mirarlo apareció un agujero real.

`locate_saves` elegía la base de cada juego **por separado**, la primera de la
lista que validara. Y validar no basta: al cruzar de juego la RAM se
reorganiza, y el buffer que queda atrás **conserva su firma y un contenido
perfectamente plausible**. Con MM corriendo, la base de OoT de la otra
disposición (`0x8011A5D0`) se prueba antes que la buena (`0x8076D400`), así que
si aún quedan restos ahí, gana — y el overlay pasa a leer los rupias y los
corazones de una foto vieja durante el resto de la sesión.

Lo que lo descarta no es el contenido sino **dónde está**: el juego que corre
tiene su save en la zona baja y el otro en la alta, siempre. Así que de las dos
bases, **exactamente una** cae por debajo de `RDRAM_MID`. Las dos abajo
significa que una es un resto.

Ahora se elige el **par** que valide y encaje, el barrido de respaldo prefiere
el que empareja con lo que ya hay, y la revalidación de cada sondeo lo
comprueba también — si no, un par malo se quedaría en la caché para siempre.
El umbral (`0x80300000`) va en el hueco vacío entre las bajas (`0x8011`–`0x801F`)
y las altas (`0x8044`–`0x8076`), con margen a los dos lados.

### Dos columnas

Pedido el 14 ago: la rejilla de items sola a la izquierda, y a la derecha las
tres listas apiladas —progreso por región, pendientes de la zona, actividad—.
Es el reparto que tiene sentido: **la rejilla es la única que gana con el
ancho**, porque `fitItems` dimensiona la celda a la caja que le den; las listas
sólo necesitan leerse. Por eso se lleva la columna algo más ancha (1,1 contra
1).

El intercambio de columnas se hizo **en el marcado, no con `order` de CSS**,
para que el orden de lectura y el visual no se separen — y de paso decide bien
qué va primero cuando por debajo de 1100 px todo cae a una sola columna.

Lo que costó no fue mover las tarjetas sino el alto. Los `max-height` de los
paneles restan una constante que es el cromo de alrededor, y con tres tarjetas
en una columna hay dos cabeceras y dos huecos más. Se **midió** en vez de
ajustar a ojo: a 1080p el body daba 1132 contra un viewport de 985, y de los
147 sobrantes la mayoría eran del menú **Display options abierto** (236 px).
Plegado —el interruptor de spoilers está fuera, que es lo único que hay que
alcanzar con prisa— y con `pane-third` en `(100vh − 604px) / 3` y `pane-tall`
en `100vh − 438px`, la vista cabe exacta: **985 = 985**, sin barra.

Verificado además que sigue bien lo que esta zona rompe fácil: los cinco
paneles sueltos no dejan contenedores vacíos, la ventana estrecha cae a una
columna sin scroll horizontal, y `/p/items` a 400×600 —tamaño de fuente de
OBS— sigue entrando sin scroll.

### Ocultar las regiones completadas

`?done=hide`, o el selector **Regions** de las opciones. Con la lista llena de
zonas terminadas, lo que queda por hacer se pierde entre ellas.

**Lo hecho se define por el filtro que tengas puesto.** Con «sólo lo
importante», una región cuenta como terminada cuando lo están sus checks
importantes, aunque le quede relleno. Si no, las dos opciones se pelearían y el
panel enseñaría regiones a `3 / 3` bajo un título que dice que están
pendientes.

Y se dice, como con lo demás: `165 regions not started · 1 completed`. Si al
ocultarlas no queda ninguna, el panel no se queda mudo — pone *every region you
have touched is done*, que no es lo mismo que «no hay datos».

### El panel de regiones sólo enseña donde has estado, y lo dice

Salió de que el usuario contara: con el filtro de importantes veía tres
regiones sumando **29** checks, y la cabecera decía **4 / 670**. Parecían faltar
ubicaciones.

No faltaban. El panel lista **sólo las regiones donde has hecho algo** (`if
got:`), y las cuentas cuadran exactas:

```
  5 regiones mostradas       ->  29 importantes
165 regiones sin progreso    -> 641 importantes
                                ---
                                670
```

El fallo era de presentación: no decía nada de las 165 que se estaba callando.
Ahora pone `165 regions not started` debajo, con el recuento acorde al filtro
—109 regiones tienen algún check importante, de 170 en total—. No aparece con
`?game=`, porque ahí el recuento y la lista no hablarían del mismo conjunto.

> Que el usuario tuviera que sumar a mano para entender un panel es la señal.
> El número que falta casi siempre es «cuántos no estoy enseñando».

### Los setups de escena: por qué Hyrule Field parecía roto

**Hecho el 14 ago 2026**, de un fallo que reportó el usuario: en Hyrule Field
cortaba un arbusto, el feed lo cantaba, y en los pendientes seguía saliendo
uno con casi el mismo nombre. Parecía que el tracker no se enteraba.

No era eso. Son **dos arbustos distintos**:

```
Hyrule Field Bush 09               setup=1  actor=39  bitpos=2438
Hyrule Field Grass Pack 3 Bush 09  setup=0  actor=60  bitpos=2322
```

Una escena de OoT existe en varias versiones —los *headers alternativos*:
niño/adulto, día/noche— y **cada una tiene sus propios actores**, luego sus
propios checks. Hyrule Field tiene tres (setups 0, 1 y 2). Sólo una está
cargada, así que la mitad de lo que el panel listaba era inalcanzable en ese
momento y se quedaba pendiente para siempre. Lo que lo hacía parecer un fallo
de detección es que los nombres se parecen y **los números coinciden** —07,
08, 09, 11, 12 en las dos familias—, así que se leen como el mismo sitio.

Es el mismo tipo de fallo que ya se arregló con Master Quest, que sí se
filtraba.

**De dónde sale el setup.** `gSaveContext.sceneSetupId`, y no está en el save
sino en el `SaveContext` que lo envuelve:

```
ASSERT_OFFSET(OotSaveContext, sceneSetupId, 0x1360)   OotSaveContext{OotSave save; …}
ASSERT_OFFSET(MmSaveContext,  sceneSetupId, 0x3cac)   MmSaveContext{MmSave save; …}
```

La base de MM del proyecto es `MmSave+0x08`, así que ahí va `0x3CAC − 8`.
Verificado en los dos volcados: con ese offset MM lee 0, y tomando la base
como `MmSave` lee basura. El `0x1360` de OoT cae dentro de lo que el POC
llamaba «flags temporales de la escena activa» (`+0x1354`…), que encaja: esa
zona no es del `OotSave`, es del `SaveContext` de detrás.

**Pero el pedido no es el cargado.** OoTMM resuelve uno a otro en
`oot/room.c`: si la escena no tiene ese header alternativo, baja al mayor que
exista y si no cae a 0; y por encima de 3 es una cutscene y usa 0. El
resultado vive en `g.sceneSetupId`, que está en el payload sin dirección
conocida, así que `setup_loaded()` **repite la resolución** usando los setups
que mencionan los propios xflags de la escena.

```
sceneSetupId=0 -> 0    sceneSetupId=2 -> 2    sceneSetupId=7 -> 0
sceneSetupId=1 -> 1    sceneSetupId=3 -> 2    (HYRULE_FIELD tiene 0,1,2)
```

**El guardia que evita el fallo simétrico.** Si el setup resuelto no está
entre los que conocemos —una escena cuyos únicos xflags vivan en un header
alternativo—, `setup_loaded` devuelve `None` y **no se filtra nada**. Sin eso,
el panel se vaciaría entero, que es peor que enseñar sobras.

Medido en Hyrule Field, 177 pendientes:

| `sceneSetupId` | en la lista | `Bush NN` | `Grass Pack … Bush` | apartados |
|---|---|---|---|---|
| 0 | 67 | 0 | 48 | 110 |
| 1 | 108 | 58 | 0 | 69 |

O sea: en el caso del usuario —el feed cantaba `Bush NN`, luego setup 1— los
`Grass Pack` desaparecen de los pendientes, que es justo lo que sobraba.

**No se quitan de los totales, y se dice cuántos son.** Los checks de otro
setup existen de verdad y son alcanzables volviendo con la otra edad, así que
sólo se filtra el panel de «lo que queda aquí», y debajo se lee `and 83 more ·
69 in another setup of this scene`. Esconderlos sin decirlo es la trampa que
este proyecto ya ha pisado dos veces.

> Y de paso, **`area cleared` tenía que querer decir cleared**. Con la lista
> vacía el panel decía siempre eso, aunque estuviera vacía por el filtro. Ahora
> distingue las tres: `nothing for this version of the scene · N in another
> setup`, `nothing important left · N junk`, y `area cleared` sólo cuando lo
> está. El de relleno era un fallo que ya estaba ahí: con `junk=hide` y sólo
> relleno pendiente, la lista salía vacía **sin ningún mensaje**, porque el
> contador miraba la lista sin filtrar.

### Las vacas, y el misterio del campo `unk` resuelto de paso

**Hecho el 14 ago**, y salió de una queja del usuario: dentro de la gruta de la
vaca, con el filtro de importantes, el panel no listaba **la vaca** — que era
justo lo que le quedaba por sacar. No salía porque los `cow_flags` eran de los
80 checks sin dirección, y sin dirección no entran en el panel.

El macro que faltaba está en `combo/save.h`:

```c
#define SAVE_EXTRA_RECORD(type, index) (gOotSave + 0xd4 + 0x1c*(index) + 0x10)
#define gCowFlags   SAVE_EXTRA_RECORD(u32, 9)
```

Y `0xD4 + N*0x1C + 0x10` es **el campo `unk` de la escena N** de la tabla de
flags de OoT. Es decir: OoTMM se guarda una veintena de u32 propios metiéndolos
en el hueco que OoT vanilla no usa de cada escena.

> **Eso cierra el «cabo suelto: el campo `unk`»** que llevaba días abierto al
> final de este documento. Los items que encendían bits en el `unk` de **dos**
> escenas a la vez estaban escribiendo dos de estos registros, y el par medido
> lo confirma: Cojiro tocaba las escenas 0 y 10, que son exactamente
> `gOotExtraTrade` (índice 0) y `gOotExtraTradeSave` (índice 10). No había
> ninguna regla geométrica que deducir; era una tabla de índices.

`gCowFlags` es el índice 9, así que vive en `oot_base + 0x1E0`, es un `u32`
con `1 << id`, y **las vacas de los dos juegos comparten el mismo campo** —
está en el save de OoT corra el que corra, que ya se localiza siempre.

Con eso, **18 de 18 `cow_flags` resueltos** y los checks pendientes de mapear
bajan de 80 a **62**. Los 18 dan 18 pares `(addr, bit)` distintos.

> **Derivado, no medido.** En los tres volcados `oot_base + 0x1E0` vale 0, que
> es coherente —no se ha ordeñado ninguna vaca— pero no prueba nada, igual que
> pasó con `gsFlags`. **Predicción falsable:** al ordeñar la vaca de atrás de
> la gruta de Termina Field debe encenderse el **bit 20** de `0x…1E0`; la de
> delante es el 19, y las tres de Romani Ranch los bits 16, 17 y 18.

### La sala filtra, y las grutas

**Corregido el 14 ago**, de estar dentro de una gruta y ver 440 pendientes:
`Remaining in GROTTOS · room 10` seguido de todas las grutas del juego.

Dos cosas. La primera es que **la sala tenía que filtrar, no sólo ordenar** —
que es lo que el backlog pedía desde el principio, «afinar los pendientes de la
escena entera a la habitación en la que estás»—. Se dejó como reordenación por
prudencia, y en una escena normal se nota poco; en `GROTTOS`, que es **una sola
escena con todas las grutas del juego dentro**, la diferencia es entre útil e
inservible. Lo que no lleva sala —cofres, NPC, tiendas— nunca se descarta.

La segunda son los `0x20 | grottoData` de `comboXflagInit`. Ésos no son un
número de sala, así que no se pueden comparar… salvo que sepas si estás **en**
la sala de las grutas genéricas o no. Y eso **sale de los datos, sin constante
ninguna**:

> La sala genérica es la que **no tiene checks propios**, precisamente porque
> sus actores se renumeraron a `0x20 | …`. En `GROTTOS` de MM las salas con
> checks son 0, 2, 5, 6 y 9–15 — no hay 4 — y 4 es exactamente la que
> `comboXflagInit` reescribe. Así que si estás en una sala que la escena
> reconoce como suya, **ninguno de los `0x20 |` puede ser tuyo**.

Medido en la gruta de la vaca de Termina Field (sala 10): de **452 a 99**. Los
99 son los 76 de esa sala más 23 cofres y NPC que no llevan sala — el cabo
suelto que ya estaba anotado. Y en la sala genérica (4) no se filtra ninguno de
ellos, que es lo correcto: allí todos son candidatos.

En una escena normal hace lo que se esperaba: Templo del Agua, sala 21, pasa de
42 a 21 —6 de la sala y 15 sin sala— y dice `21 in other rooms`.

### Y de propina, la sala

Del mismo `PlayState` sale `roomCtx.curRoom.num`, y **sólo los xflags llevan
sala** en `checks.json` (4.440 de 6.043). Con eso los pendientes de la sala en
la que estás salen **primero y marcados**, y el título dice «· room N» cuando
la escena tiene más de una.

Dos decisiones, las dos por el mismo motivo —que un filtro que esconde cosas
sin decirlo es peor que no filtrar—:

- **Se reordena, no se filtra.** Los cofres, los NPC y las tiendas no tienen
  sala; filtrando desaparecerían todos. Medido en el Templo del Agua con el
  jugador en la sala 21: 42 pendientes, 6 marcados y arriba, y los 15 sin sala
  siguen en la lista.
- **En las grutas no se aplica.** Ahí el `room` del xflag es el truco
  `0x20 | grottoData` de `comboXflagInit`, no un número de sala, mientras que
  `curRoom.num` vale 0. Comparándolos, los 311 checks de gruta saldrían todos
  como «de otra sala». Se marcan como sin sala y listo.

### Recolocación de bases: por qué `checks.json` lleva ancla

Las direcciones de `checks.json` son absolutas, y las bases **se mueven** al cruzar entre OoT y MM: la RAM se reorganiza entera, que es justo la razón de que exista `locate_saves`. Un overlay que corre durante horas tiene que rebasar, así que cada check lleva además `anchor` + `off`:

| Ancla | Se resuelve | Cuelga de |
|---|---|---|
| `oot` | firma `ZELDAZ` | flags de escena de OoT, `gsFlags` |
| `mm` | firma `ZELDA3` | flags de escena de MM |
| `custom` | desplazamiento fijo desde `mm` | los 4.751 xflags y los bitmaps custom |

El custom save no tiene firma propia, pero cuelga del buffer de MM por una distancia que es constante de versión: los dos son globales de la misma build, así que se mueven juntos.

### La firma no basta para localizar un save

Salió jugando otra seed: con OoT corriendo, el panel de MM se llenó de basura —`SKULLTULAS PANTANO 7680`, cuando el máximo es 30— y las regiones de MM inventaban progreso.

`locate_saves` buscaba la firma `ZELDA3` y **se quedaba con la primera coincidencia por dirección, sin comprobar nada**. Pero la firma aparece también en copias estáticas y en buffers viejos, así que la primera no suele ser la viva. En las sesiones anteriores funcionaba de chiripa: la base conocida acertaba y no hacía falta escanear.

Ahora cada candidata pasa una comprobación de plausibilidad barata, con campos cuyo rango se conoce:

| Campo | Invariante |
|---|---|
| `healthCapacity` | mayor que cero, hasta 20 corazones, y **múltiplo de 0x10** |
| `health` | entre cero y la capacidad |
| `rupees` | de 0 a 9999 |
| skulltulas de pantano y océano (MM) | 30 como mucho |

Verificado contra los dos volcados: en el de OoT descarta la copia estática de MM (`0x80442248`) y se queda con la viva (`0x8044BE18`); en el de MM descarta `0x801C6954` y elige `0x801EF678`. Y al cruzar de juego relocaliza sola, en los dos sentidos.

> **De paso, un problema peor.** `locate_saves` corre en cada sondeo, y cuando una base no está entre las conocidas **escanea 8 MB de RDRAM**. Dos veces por segundo. Con las bases movidas, cada sondeo era un barrido de la memoria entera. Ahora las bases se cachean y sólo se relocalizan cuando dejan de validar.

### La medida de confianza

`gSharedCustomSave` sólo está localizado con **OoT corriendo**; con MM su dirección es otra y no se ha despejado. En vez de enseñar basura, el overlay mide qué fracción de los bits encendidos cae en un check conocido y por debajo del 90% marca el panel como no fiable. Si la base está mal, los bits caen donde no hay nada mapeado y la medida se hunde sola.

Se mide **sólo sobre los tramos de xflags**, que son bitmap puro. Medirla sobre el bloque entero daba 0.93 con la base correcta, porque dentro hay campos que no son flags de check —el bitfield de cola de `OotCustomSave`, `mm.halfDays`, los contadores— cuyos bits encendidos son legítimos pero no mapean a nada. Acotada a los xflags da 1.0.

**La fracción sola no basta, y esto es un agujero que estuvo abierto un tiempo.** Una base equivocada que caiga en una zona de ceros da 1.0 por vacuidad, sin un solo bit: eso no es un aviso, es un silencio, y el overlay se limitaba a contar menos checks sin decir nada. Ahora se compara también el número de bits con el mayor visto. Si antes había y ahora no hay ninguno, o la base es mala o has empezado partida nueva, y **lo que las distingue es que en una partida nueva se caen también los checks de escena**, que se leen por otra ancla: si esos siguen ahí y los xflags no, la base está mal.

Ese guardia fue el que dejó al descubierto que **con MM corriendo el custom save no se estaba leyendo** — y con eso localizado, se arregló. Ver abajo.

### El custom save con MM corriendo

Era la última limitación grande, y se resolvió sin tocar el emulador, usando los dos volcados que ya había: son de la misma partida y **el custom save es compartido**, así que su contenido tiene que aparecer literalmente en las dos memorias. Buscando el bloque de la RAM de OoT dentro de la de MM sale **una única coincidencia**, y los offsets no-cero cuadran uno a uno: `0xd6`, `0x1b2..0x1c1` de xflags, `0x31a` shops, `0x376` el bitfield de cola de `OotCustomSave`, `0x6f4` `halfDays`. Las únicas diferencias son los dos checks que el jugador hizo entre un volcado y otro.

La regla resultó ser simétrica: **`gSharedCustomSave` va justo delante del buffer del juego que NO está corriendo.**

| Juego activo | Buffer del otro | Custom save | Distancia |
|---|---|---|---|
| OoT | MM en `0x8044BE18` | `0x8044B570` | `0x8A8` |
| MM | OoT en `0x8076C4F0` | `0x8076BC50` | `0x8A0` |

Las distancias difieren porque lo que hay en medio es el save del otro juego, y no ocupan lo mismo. El sondeo prueba las dos direcciones y se queda con la que más bits mapeados da, así que no depende de acertar a la primera.

**La comprobación que lo cierra**: con el volcado de MM el tracker pasa de 5 checks a **20**, y bajo OoT da 18. La diferencia de 2 es exactamente la de los dos volcados — un bit de xflag en `0x300` y el `Song of Healing` en `0x6dc`.

### Gotchas que salieron de construirlo

- **`read_block` viaja en palabras de 4 bytes.** Pedir un campo en una dirección no alineada (`info.sceneId` está en `+0x66`) devuelve basura con el enlace real. Hay que leer la palabra que lo contiene y sacar el halfword de dentro. Con un enlace falso sobre un volcado esto **no** se nota, así que es de los que pasan el test y fallan en vivo.
- **El primer sondeo tiene que fijar la línea base en silencio**, o el feed arranca escupiendo de golpe los cientos de checks que ya llevabas.
- **Sombreado de variables.** El bucle de regiones usaba `scene` como variable y pisaba el id de escena que venía del sondeo; después del bucle valía el *nombre* de la última región, así que `scene_id == scene` comparaba un entero contra un string y la lista de pendientes salía siempre vacía, sin error.
- **La pista del medidor no puede parecerse al relleno.** Con la pista a un 26% del tono, una barra al 0,4% se leía como llena. En un overlay de stream eso es desinformar a quien mira.
- **Los controles que generan URLs tienen que arrancar leyendo la URL.** Los `<select>` de la vista de director salían con su valor por defecto del HTML, así que abrir `/?spoiler=off` generaba enlaces sin `spoiler=off`: la opción se veía puesta y no se propagaba.
- **Colisión de clases entre dos componentes.** Las filas de región llevan `row-oot` / `row-mm` para heredar el color del juego, y el contenedor de la rejilla de items llevaba las mismas. Al ponerle `display:flex` a esas clases para colocar los grupos en paralelo, **desaparecieron los medidores de todas las regiones**: la fila dejó de ser un grid y el `.meter` se encogió a nada. Una clase que sólo aporta variables de color no puede usarse además como gancho de maquetación; ahora el contenedor de la rejilla es `.gamegrid`.
- **Zona muerta temporal de `const`.** El bloque que monta el modo panel usaba `GAME` para titular, y `const GAME` estaba declarado más abajo: `Cannot access 'GAME' before initialization` reventaba el script entero y los paneles salían vacíos, sin nada roto a la vista. La captura sólo mostraba una tarjeta vacía; lo que lo delató fue leer la consola con `--enable-logging=stderr`.
- **Cuidado con las capturas headless de Edge en pantallas con escalado.** Pedir `--window-size=420` daba un viewport de 504 px CSS y un PNG de 420, recortando la derecha: parecía que faltaban los contadores de las regiones. No había tal fallo. Antes de arreglar algo que sólo se ve en una captura, **medir el DOM** (`--dump-dom` con una sonda que escriba las medidas en el `<title>`) — y borrar la sonda después.

### Técnica que conviene usar a partir de ahora

**Savestates.** Guardar un savestate antes de coger un check permite volcar, recargar, y tener el check otra vez sin coger. Mapear ubicaciones deja de gastar la partida y el experimento A/B pasa a ser exacto: mismo estado de partida, única diferencia el check. Se descubrió tarde; habría ahorrado varias horas.

---

## La colocación sale de la ROM: el spoiler ya no hace falta

**Hecho el 13 ago 2026.** Salió de preguntar si en vez de cargar un spoiler se
puede leer de la ROM qué item hay en cada sitio. Se puede, y es lo que hace
ahora `placement.py`: **5.371 ubicaciones con su item, sin pedir nada a nadie**.

### La tabla

`comboItemOverride()` (`src/common/item/item.c`) resuelve una consulta a un
item, y lee de un fichero de la ROM, `COMBO_VROM_CHECKS`:

```c
typedef struct ComboOverrideData {   /* 16 bytes, ORDENADA por key */
    u32 key;      /* (ovType << 24) | (sceneId << 16) | (roomId << 8) | id */
    s16 player;   /* de quien es el item, para multi */
    u16 value;    /* <-- el item, un GI */
    s16 giCloak;
    s16 unused[3];
} ComboOverrideData;
```

El juego la recorre con **búsqueda binaria** sobre la clave, con una caché de
64 entradas. Nosotros podemos leerla entera de una vez.

**Dónde está, y esta es la mejor noticia:** `COMBO_VROM_CHECKS` es
`COMBO_EXTRA_DMA_VROM | 0x00400000` en el build de OoT y `| 0x00500000` en el
de MM (`combo/defs.h`), o sea **`0xF0400000` y `0xF0500000`**. Constantes
estructurales, no direcciones que se muevan con cada versión como los
`0x80b0f00` de las tablas de xflags. Y se leen con `rom.read_extra_vrom`, que
ya existe.

### Lo medido sobre la seed f5PCTnhD

```
0xF0400000  build de OoT   36.000 bytes = 2250 entradas
0xF0500000  build de MM    44.320 bytes = 2770 entradas
                                          ----
                            menos 2 centinelas (ovType 0xFF)  = 5018
```

**5018 es exactamente el número de ubicaciones del spoiler log.** Las dos
tablas salen ordenadas por clave y sin claves repetidas.

Y el reparto por `ovType` da los tres bloques que faltaban por mapear:

| ovType | | OoT | MM | |
|---|---|---|---|---|
| 1 | chest | 179 | 188 | |
| 2 | collectible | 37 | 22 | |
| 3 | npc | 95 | 123 | |
| 4 | gs | **100** | — | los 100 vanilla, clavado |
| 5 | sf | — | **29** | las stray fairies que faltan |
| 6 | cow | 9 | 8 | |
| 7 | shop | 64 | 22 | |
| 8 | scrub | 36 | — | |
| 9 | sr | 80 | — | |
| 10 | fish | **33** | — | los `caughtFishFlags` que faltan |
| 16–27 | xflag0–11 | 1225+ | 1685+ | |

> Ojo: esto **no** resuelve los 80 checks pendientes. Están pendientes porque
> no sabemos **dónde vive su flag**, no porque no sepamos qué item hay. Lo que
> hace la tabla es enumerarlos exactamente, y de paso confirmar los conteos.

### La clave, y la trampa que tuvo

La clave de cada check, tal como la forma `placement.override_key`:

```
xflags:  ov = 0x10 + slice
         room = (room & 0x3F) | ((setup & 3) << 6)      <- igual que comboXflagItemQuery
         key = (ov << 24) | (scene << 16) | (room << 8) | actor
```

**Los demás tipos no llevan todos la escena.** Sólo `chest`, `collectible` y
`sf` la usan; en `npc`, `gs`, `cow`, `shop`, `scrub`, `sr` y `fish` el byte de
escena es **0**, porque son espacios de id globales. Se vio mirando las claves
reales de la ROM, tras un primer intento en que todo eso fallaba en bloque.

> **El id de la clave es el índice global del bitmap, y `checks.json` no lo
> tenía.** En `npc`, `gs`, `shop`, `scrub` y `sr`, `mkchecks.py` reescribe
> `bit` para dejar el bit dentro de su byte, y con eso 61 claves acababan
> reclamadas por varios checks a la vez: `Hatch Chicken`, `Malon Egg`,
> `Lost Woods Target` y `Saria's Song` compartían `0x03000000`. El id bueno
> sólo existe en el momento de leer el CSV, así que ahora se guarda aparte en
> el campo **`csv_id`** y la clave se forma con ese.

### La prueba de que es la colocación de verdad

Dos, y la segunda es la que vale.

**Uno**: si la tabla es lo que creemos, cada `gi` tiene que corresponder a un
solo nombre de item del spoiler. Sobre los tipos con clave 1:1 salen **63 `gi`
distintos y ni un conflicto real**. Los dos que aparecen con varios nombres son
`Small Key (…)` y `Stray Fairy (…)`, que el generador nombra según la mazmorra
— comportamiento correcto, no error de lectura.

**Dos, la buena**: lo que importa no es que el texto coincida letra a letra,
sino que **la clasificación de relleno salga igual**, que es para lo que se
usa. Sobre las 5.018 ubicaciones que tienen item por las dos vías:

```
clasificacion de relleno, ROM contra spoiler:  5018 coinciden, 0 discrepan  (100%)
nombres:  4755 identicos · 17 sólo cambian el sufijo (OoT)/(MM) · 246 distintos
```

Los 246 nombres distintos no son errores: el spoiler dice `Progressive Sword`
y la ROM `Kokiri Sword`, el spoiler `Gold Rupee` y la ROM `Huge Rupee`. La ROM
nombra el item concreto y el spoiler la entrada del pool.

> **Las 34 discrepancias que sí hubo, y por qué importaban.** Salieron cuatro
> casos: `Milk` contra `some Lon Lon Milk` y `1 Bomb` contra `Bomb`. Los dos
> son de forma, no de contenido, pero los dos **hacían pasar por importante un
> relleno**. Se arreglaron por los dos lados: `limpia_nombre` quita también el
> `some` inicial, y los patrones de relleno admiten el singular y el
> `Lon Lon`. De ahí sale el 100%.

### Cobertura, y lo que queda fuera

```
checks activos:                    5074
  con direccion:                   4995
  con item de la ROM:              4939   (98,9%)
sin item, en total:                 672
  de esos, Master Quest:            616   correcto: no existen en esta seed
  activos y con direccion:           56   (1,1%)
```

Los 56 son 25 `tree`, 7 `grass`, 5 `crate`, 5 `butterfly`, 4 `pot`, 3
`boulder-silver`, 3 `collectible`, 2 `rock`, 1 `snowball` y 1 `npc`. Se quedan
sin item y quien los consuma lo sabe: `is_junk(None)` da `False`, así que
cuentan como importantes, que es el lado seguro por el que fallar.

### Cómo queda montado

| | |
|---|---|
| `placement.py` | lee las dos tablas de la ROM y `data/gi.yml`, y forma las claves |
| `data/gi.yml` | copia de `data/defs/gi.yml` del repo; el índice `gi` es la posición + 1 |
| `mkchecks.py` | guarda `csv_id`, y escribe `item`, `item_id`, `gi` y `ovkey` en cada fila |
| `overlay.py` | `rom_items` de `checks.json`; el spoiler cargado a mano se pone **encima** |

El botón de cargar spoiler **se queda**, pero pasa a ser el camino de
respaldo: sirve cuando de la ROM no se puede leer la tabla. La vista de
director dice cuál está en uso — «5.371 items leídos de la ROM · no hace falta
spoiler».

Medido con el overlay servido sobre el volcado y **sin spoiler ninguno**:
`can_filter` sale a `true` solo, y «sólo lo importante» da **4 / 612** y 2
pendientes en Kokiri Forest — los mismos números exactos que daba cargando el
spoiler a mano.

### Y una precisión sobre «más estable con futuras versiones»

Sí, pero conviene no venderlo de más:

- **Sí es más estable en la dirección**: `0xF0400000` es una constante
  estructural, frente a las VROM de las tablas de xflags, que son de v32.0 y
  ya han roto una vez con una seed de otra versión.
- **No es independiente de la versión**: el formato de la clave, la numeración
  de `ovType` y sobre todo el índice `gi` pueden cambiar entre versiones — un
  item nuevo en medio de la lista desplaza todos los que van detrás.

Y una nota que no es técnica: leer la colocación de la ROM **no da menos
información que el spoiler, da la misma**. Lo que se gana es que no hay
fichero que encontrar, cargar ni validar; no que el tracker sepa menos.

## Lectura del inventario

El tracker de items no necesita el sistema de checks: lee el inventario directamente del save context, que es más inmediato (no espera a cambiar de escena) y cubre los dos juegos.

### Mapa de OoT

Sacado de `combo/oot/save.h` y validado en juego. Ver `inventory.py`.

```
items[24]  +0x74     ammo[15]  +0x8C     equipment +0x9C    upgrades +0xA0
questItems +0xA4     dungeonItems +0xA8  goldTokens +0xD0
```

`equipment` son cuatro nibbles (espadas, escudos, túnicas, botas), y `upgrades` ocho campos de 2–3 bits (carcaj, bolsa de bombas, fuerza, escama, cartera, bolsa de balas, palos, nueces). Los bitfields se leen a la manera de MIPS big endian: **el primer campo declarado en la struct ocupa los bits más altos**.

### Mapa de MM

No estaba en ningún sitio: se ancló cazando **dos máscaras**.

```
Deku   (máscara 5,  slot 29)  ->  +0x85
Romani (máscara 12, slot 36)  ->  +0x8C     diferencia de 7 slots
```

Esa diferencia coincide con el orden real de máscaras de MM, y de ahí sale `items[48] = +0x68`. Con el layout de `MmInventory` del header, el resto por resta:

```
items[48] +0x68   ammo[24] +0x98   upgrades +0xB0   quest +0xB4
dungeonItems[10] +0xB8   dungeonKeys[9] +0xC2   strayFairies[10] +0xCC
skullCountSwamp +0xEB8   skullCountOcean +0xEBA
```

Comprobado por tres vías independientes: el slot 26 de un volcado valía `0x47` (Blast Mask, que salió del cofre de Mido); `quest` tenía el bit 12 (Song of Time, la del Skull Kid); y los trozos de corazón en los 4 bits altos de esa palabra.

**`MmUpgrades` tiene el mismo layout que `OotSaveUpgrades`** (sólo cambia `dive` por `scale`). Confirmado: al mejorar los palos deku el `u32` subió exactamente `1<<17`, que es donde cae `dekuStick` en ambos.

### La tabla de ids: 325 items sin cazar ninguno

`packages/generator/include/combo/data/items.h` (copia en `data/ref/`) define los `ITEM_OOT_*` y `ITEM_MM_*`, **y el valor guardado en `items[]` es directamente ese id**. Validado contra los ocho ids cazados en vivo:

```
ITEM_MM_BOMBCHU 0x07 · ITEM_MM_POWDER_KEG 0x0c · ITEM_MM_MASK_DEKU 0x32
ITEM_MM_MASK_ROMANI 0x3c · ITEM_MM_MASK_BLAST 0x47 · ITEM_MM_BOOTS_HOVER 0xb2
ITEM_OOT_POWDER_KEG 0xa7 · ITEM_OOT_COJIRO 0x2f
```

162 items de OoT y 163 de MM quedan nombrados de golpe. **No hace falta cazar item por item para etiquetar el inventario**: basta con leer el id.

### Dos cosas que sólo se ven jugando

- **OoTMM sincroniza las mejoras entre juegos.** Las mejoras de nueces y de palos tocaron `upgrades` y `ammo` en OoT *y* en MM a la vez. Dos casos independientes, así que es el comportamiento normal.
- **Los items se cruzan de inventario.** El Powder Keg (de MM) ocupa el slot de bombas en OoT con id `0xA7`; las Hover Boots (de OoT) aparecen en el slot 17 de MM con id `0xB2`. Para el tracker: **no basta con mirar si un slot está ocupado, hay que leer el id**.

### Verificaciones en vivo

Quince items cogidos con el tracker mirando, cada uno confirmando una estructura distinta:

| Item | Qué confirmó |
|---|---|
| Minuet · Serenade · Sun's Song | `questItems` de OoT |
| Recovery Heart | `health` |
| Large Magic Jar | (dejó `+0x537` sin identificar) |
| Mejora de nueces · de palos | `upgrades` + `ammo`, y la sincronización entre juegos |
| Deku Mask · Romani Mask | `items[48]` de MM y el orden de las 24 máscaras |
| Song of Time · 2 trozos de corazón | `quest` de MM |
| Restos de Twinmold | `quest` de MM con un objeto clave |
| Skulltula del pantano | contador `+0xEB8` |
| Piece of Heart (tienda) | bitmap `shops` del custom save |
| Cojiro | slot 22 de OoT |
| Giant's Knife | nibble de espadas + `swordHealth` |
| Espada Kokiri | bit 0 del nibble de espadas |
| Hover Boots | nibble de botas + slot 17 de MM |
| Powder Keg | slot 12 de MM + slot de bombas en OoT |
| Bolsa de bombchus | slot 7 de MM + slot 8 de OoT |

### El cazador

`ootmm.py items` lee los dos saves en bucle y canta cada cambio. Tres cosas lo hacen usable:

- **Localiza las bases por firma y revalida en cada lectura.** Si detecta que la firma se movió, relocaliza sola: es lo que permite cruzar de OoT a MM sin reiniciar nada. La comprobación es gratis, porque la firma viene dentro del bloque que ya se lee.
- **Calibra el ruido al arrancar.** Seis segundos observando qué se mueve solo, y eso queda silenciado. El bloque incluye posiciones y temporizadores que si no ahogan el log.
- **Auto-silencia lo que insiste.** Un byte sin identificar que cambia más de tres veces se da por contador y se calla. Un item se coge una vez; un reloj no.

Todo lo que no reconoce sale con offset y dirección, así que ningún item pasa inadvertido aunque escriba en un sitio nuevo. Es lo que permitió cazar los quince de arriba.

### Cabo suelto: el campo `unk`

Varios items encienden bits en el campo `unk` de la tabla de escenas, que en OoT vanilla no se usa:

| Item | Escenas | Bit |
|---|---|---|
| Minuet / Blast Mask | 0 y 10 | 27 |
| Cojiro | 0 y 10 | 2 |
| Powder Keg | 1 y 20 | 24 |

Siempre **dos** escenas y el mismo bit en ambas, pero el par cambia según el item, y en el caso del Powder Keg los valores base eran distintos entre las dos escenas (así que no son copias idénticas). Con tres muestras no da para deducir la regla. Merece la pena volver aquí: si el índice sigue algún orden, es otra vía para mapear checks.

> **RESUELTO el 14 ago 2026, y no había regla que deducir: era una tabla.**
> `SAVE_EXTRA_RECORD(type, index)` de `combo/save.h` es
> `gOotSave + 0xd4 + 0x1c*index + 0x10`, o sea **el `unk` de la escena
> `index`**. OoTMM mete ahí veintiún u32 propios. Los pares que se midieron
> cuadran: Cojiro en las escenas 0 y 10 son `gOotExtraTrade` (índice 0) y
> `gOotExtraTradeSave` (índice 10); el Powder Keg en 1 y 20 son
> `gOotExtraItems` y `gMmExtraAmmo`.
>
> Y sí era «otra vía para mapear checks»: de ahí salió `gCowFlags` (índice 9)
> y con él los 18 `cow_flags`. Ver la sección de las vacas más arriba. Quedan
> sin usar `gMmOwlFlags` (11) y los cinco `gOotSilverRupeeCounts` (13–17), que
> son los siguientes candidatos si hiciera falta.

---

## El `.exe`: repartirlo sin pedir Python

**Hecho el 14 ago 2026.** `python -m PyInstaller ootmm.spec` deja
`dist/ootmm-tracker.exe`, **8,5 MB**, un solo fichero y sin nada que instalar.

Lo que hacía falta no era empaquetar, que es una línea, sino separar dos cosas
que hasta ahora eran la misma: **lo que viaja con el programa** y **lo que el
programa produce**. Dentro del `.exe` dejan de estar en el mismo sitio, y cada
una falla distinto.

### `paths.py`, las dos carpetas

| | Desde el código | Desde el `.exe` |
|---|---|---|
| `paths.res(...)` — lo que viaja | la carpeta del proyecto | `sys._MEIPASS`, el temporal donde se desempaqueta |
| `paths.user(...)` — lo que se genera | la carpeta del proyecto | `%LOCALAPPDATA%\OoTMM-Tracker\` |

Ejecutando desde el código las dos devuelven lo de siempre, así que **no cambia
nada** en el flujo de trabajo de aquí.

Lo que va en cada una:

```
res   data/ (pool CSVs, scenes.yml, npc.yml, gi.yml, ref/), overlay.html,
      Scripts/tracker.lua, icons/LEEME.md, README.md
user  checks.json, icons.json, icons.png, discover-cache.json, icons/
```

Poner los generados en `_MEIPASS` habría sido el fallo silencioso clásico: la
carpeta se borra al salir, así que **cada arranque regeneraría las tablas** —lo
único lento que hay— y nadie vería un error, sólo un tracker que tarda medio
minuto en abrir siempre.

### El subproceso que no podía funcionar

`discover.py` lanzaba los generadores con
`subprocess.run([sys.executable, "mkchecks.py", ...])`. Dentro del `.exe`
`sys.executable` **es el tracker**, no un intérprete, y no hay ningún `.py` que
pasarle: eso relanza el tracker con argumentos que no entiende.

Ahora `_generate()` importa el módulo y llama a su `main(argv)`. Los dos
`main()` pasaron a aceptar `argv` (`ap.parse_args(argv)`), que es todo el
cambio, y siguen valiendo como script suelto. `SystemExit` se captura para
conservar el código de salida, que es lo que distingue «no pude» de «hecho» —y
lo que hace que el aviso de *los checks son de otra ROM* siga saliendo.

Como se llaman por nombre, van en `hiddenimports` del `.spec`: el análisis
estático no los ve, y sin eso el `.exe` arranca perfecto y sólo falla al
cambiar de seed.

### Lo que salió mal al construirlo

- **Excluir `email` del paquete.** Parecía muerto y `http.server` lo importa.
  El tracker arrancaba entero —detección, tablas, iconos, enlace con el Lua,
  todo correcto— y reventaba con `ModuleNotFoundError: No module named 'email'`
  **al levantar el servidor**, que es lo último que pasa. La lista de
  `excludes` se quedó en `tkinter` y `unittest`; el megabyte que ahorraba lo
  otro no vale una traza así.
- **Sin argumentos no hacía nada.** El subparser es obligatorio, así que
  doble-clic = imprimir el uso y cerrar la ventana antes de que se lea. Desde
  el `.exe`, sin argumentos ahora significa `overlay`; desde el código sigue
  imprimiendo el uso.
- **La consola se la lleva el proceso al morir.** `_run()` sostiene la ventana
  con un `input()` al terminar, pero sólo si está congelado, si `stdin` es un
  terminal y si **hubo doble clic o hubo error**: escribir un subcomando en una
  consola ya deja la salida a la vista.
- **Envolver `main()` en un `try` se comió todos los mensajes de error.** Aquí
  se falla con `sys.exit("explicación")` por todas partes, y ese texto lo
  imprime el intérprete **sólo si nadie captura el `SystemExit`**. `_run()` lo
  capturaba para quedarse con el código de salida, así que cada uno de esos
  fallos pasó a ser un código y silencio — y no sólo en el `.exe`, también
  desde el código. Ahora, si `ex.code` no es un entero, se imprime a `stderr`.
  Es el fallo de siempre en versión nueva: no se rompió, se calló.
- **`--emu` equivocado no debe caer al emulador detectado.** Es una pista para
  la búsqueda, así que apuntarlo a la carpeta incorrecta instalaba el script en
  el emulador de verdad y decía que todo bien. Si se pasa a mano y no tiene
  `Config\Project64.cfg`, error.
- **Decir «instalado» de lo que no se ha escrito.** `ensure_lua()` devolvía la
  ruta en los cuatro casos, así que negarse a pisar un script ajeno se
  anunciaba igual que haberlo puesto. Ahora devuelve `(ruta, estado)` con
  `written` / `same` / `kept`, y cada uno se cuenta distinto.

### `tracker.lua` dentro del paquete

El script está en el `.exe`, así que ya no se puede copiar a mano. `ensure_lua()`
lo escribe en `Scripts\` del emulador —que `discover` ya sabía localizar— la
primera vez que arranca el overlay, y hay `ootmm-tracker.exe install-lua` para
hacerlo aparte.

> **Trampa nueva del enlace duro.** Editar `Scripts/tracker.lua` con una
> herramienta que escriba el fichero entero **rompe el enlace**: se crea un
> fichero nuevo con ese nombre y el emulador se queda con la copia vieja, sin
> que nada avise. Pasó al traducir sus dos comentarios. Después de tocarlo:
> `fsutil hardlink list`, y si hay un solo nombre, rehacerlo con
> `Remove-Item <emu>` y `New-Item -ItemType HardLink`. Y reconstruir el
> `.exe`, que lleva su propia copia dentro.

Dos guardias: **nunca pisa un script que ya esté ahí** (si difiere lo dice y no
lo toca, hay que pedirlo con `--force`), y **desde el código no escribe nada**
salvo que se lo pidan, porque aquí la copia del proyecto y la del emulador son
*el mismo fichero* por un enlace duro y sobrescribirlo lo partiría en dos
copias que luego divergen en silencio. Se escribe en binario, que es la otra
forma de no meter un BOM.

### Cómo se probó

Un `tracker.lua` **falso en Python** —cliente que se conecta al puerto y sirve
un volcado con el mismo protocolo: `PING` → `TRK1`, opcodes 2/3/4 y `0x10`—
permite correr el overlay entero de punta a punta sin emulador. Está en el
proyecto como `fakelua.py`, porque es la única forma de probar el `.exe`: el
atajo `--dump` sólo lo tienen `items` y `checks`, y además se salta el enlace,
que es justo la parte que el empaquetado podía romper. Es lo que dio la
comparación de abajo:

| Prueba | Resultado |
|---|---|
| `checks.json` regenerado desde el código | **idéntico byte a byte** al de antes de tocar nada |
| `icons.json` / `icons.png` | idénticos |
| `checks.json` que genera el `.exe` | idéntico en las 6.043 filas (sólo cambia el `rom`, por las barras de la ruta) |
| `/state.json` del `.exe` vs. del código, mismo volcado | **idéntico**, salvo `uptime` |
| `ootmm-tracker.exe checks --dump` vs. `python ootmm.py checks --dump` | salida idéntica |
| `/`, `/p/regions`, `/icons.png` servidos por el `.exe` | 200, 60.561 y 311.835 bytes |
| Arranque **en limpio** (borrando `%LOCALAPPDATA%\OoTMM-Tracker`), sin argumentos, con `ram-en-mm.bin` | detecta ROM, regenera tablas e iconos, `active: mm`, bases `MM 0x801EF678` / `OoT 0x8076C4F0`, confianza 1.0 |

La última es la que vale por todas: es exactamente lo que le pasa a quien lo
descarga.

### Lo que queda

- **Los antivirus.** Un ejecutable de PyInstaller sin firmar da falsos
  positivos; está avisado en el README. Firmarlo cuesta dinero, y la
  alternativa honesta es que quien no se fíe use el código.
- Sólo está probado en esta máquina (Windows 11, Python 3.14, PyInstaller
  6.22). El `.spec` no tiene nada de Windows, pero nadie lo ha construido en
  otro sitio.
- El arranque tarda un par de segundos en desempaquetarse, que es el precio del
  fichero único. Con `--onedir` no pasaría, a cambio de repartir una carpeta.

---

## Los nombres de items salen de `kItemNames[]`

**Hecho el 14 ago 2026.** Era el último fallo de versión que no avisaba: los
nombres venían de `data/gi.yml`, y ahí **el índice `gi` es la posición en el
fichero**, así que un item nuevo en medio corre todos los de detrás. No se
rompe: se equivoca de nombre y se calla.

### Dónde está

Está escrito en el repo, no hubo que adivinarlo:

- `packages/generator/include/combo/gi.h` → `extern const char* const kItemNames[];`
- `packages/generator/src/common/text/text.c` → `itemName = kItemNames[gi - 1];`
  (confirma el `- 1` que ya se dedujo de `data.ts`)
- `packages/generator/lib/combo/codegen.ts` lo genera recorriendo `GI` en orden,
  que es el orden de `gi.yml`.

Y vive en el **payload**, que es otro fichero de la extra DMA. De
`combo/defs.h`:

| | VROM | Se carga en | Tamaño |
|---|---|---|---|
| payload de OoT | `0xF0000000` | `0x80400000` | `0x80000` |
| payload de MM | `0xF0100000` | `0x80720000` | `0x60000` |

Como el payload se carga entero y de una pieza, **un puntero de dentro es
`PAYLOAD_RAM + offset en el fichero`**. Eso es lo que hace que se pueda leer
desde la ROM sin emulador: se resuelve el puntero restando la base.

### Localizarlo por contenido, no por dirección

Una dirección más sería una constante de versión más, o sea el problema que
esto viene a quitar. Se busca por forma: **la tirada más larga de u32
consecutivos que caen dentro del payload y apuntan todos a una cadena**.

En la seed de hoy sale exactamente una tirada de **936**, que son las 936
entradas de `gi.yml`, en los dos payloads. Pero hay una segunda tirada que
también es 100% cadenas:

```
+0x039084   936 ptrs   cadenas: 100.0%   <- kItemNames
+0x0480CC   822 ptrs   cadenas:  38.7%
+0x046308   254 ptrs   cadenas: 100.0%   <- nombres de región, para las pistas
```

Lo que las separa: **los nombres de item llevan código de color y los de
región no.** 927 de 936 traen un byte de control; los 254 no traen ninguno.
Con ese segundo filtro la identificación es inequívoca sin depender de cuál
es más larga.

### El texto, y por qué se limpia distinto en cada juego

Los dos payloads llevan **las mismas palabras con distinta codificación**:

```
OoT:  b'the \x05AMegaton Hammer'      0x05 + byte de color
MM:   b'the \x01Megaton Hammer'       un solo byte
```

Por eso el limpiador es por juego. Después, lo de siempre: quitar el artículo
y colapsar espacios, para que quede con la misma forma que escribía el
spoiler, que es contra la que están escritas las reglas del relleno.

### Lo que se midió

Con la seed que se está jugando (`dockiNAq`):

- 936 nombres leídos, `gi.yml` coincide en **901 de 927**.
- Las 26 que no: todas `Rusty Key (...)`, con la ROM dando el nombre bueno
  (`Rusty Key (Market Treasure Chest Game)`) y el fichero uno viejo
  (`Rusty Key (Treasure Chest Game)`).
- **Ninguna de esas 26 se coloca en esta seed** (0 de los 317 `gi` que usa),
  porque esa función —cerrar con llave puertas que no tienen cerradura— no
  está activada. Por eso `checks.json` sale **byte a byte idéntico** después
  del cambio, que es la mejor regresión que se podía pedir.

Y con las demás ROMs de `Downloads`, que es donde se ve para qué sirve esto:

| | nombres | acuerdo con `gi.yml` |
|---|---|---|
| 17 ficheros (`Siixg4Kf`, `7NxgFEzA`, `BIEwYjtP`…) | **829** | 136/822 |
| 11 ficheros (`dHN9YY2c`, `f5PCTnhD`, `Lunes`, `xmMVaicW`…) | 936 | 927/927 |
| `dockiNAq` | 936 | 901/927 |

Las de 829 son de una versión bastante anterior, y ahí el desfase es total:

```
gi 200   gi.yml: Dungeon Map (Jabu)     ROM: Compass (Water)
gi 600   gi.yml: Giant's Mask           ROM: Goron Lullaby
gi 800   gi.yml: Soul of Lulu           ROM: Nayru's Love
```

O sea que el fallo no era una hipótesis: estaba vivo en media carpeta.

### Cuando no puede, lo dice

`gi.yml` se queda para el **símbolo** (`OOT_BOMBS_5`), que no sobrevive a la
compilación y por tanto no está en la ROM. Pero sólo se usa si el fichero
sigue alineado: se comparan sus nombres con los de la ROM y por debajo del
**90% de acuerdo** se descarta el `item_id` y se explica por qué.

La prueba de que el guardia funciona es meter el fallo a mano. Con un item
falso insertado en la posición 20 de `gi.yml`:

```
names: 936 read from the ROM's kItemNames; gi.yml agrees on 29/928
  WARNING: data/gi.yml does not line up with this ROM.
```

y los nombres siguen saliendo bien, porque ya no dependen de ese fichero:
`gi 24` da `Spooky Mask`, que es lo que hay, en vez del `Skull Mask` corrido
que habría dado antes.

Sin payload localizable se cae a `gi.yml` **diciéndolo**, y una ROM que no sea
de OoTMM —probado con Super Mario 64 y con la OoT vanilla— devuelve `None` en
vez de reventar: su cabecera de extra DMA no existe y `struct.error` se
escapaba por no estar en el `except`.

### Lo que esto NO arregla

Las **tablas de xflags** siguen siendo constantes de `custom.h` de v32.0. Esas
17 siguen abortando en `mkchecks` con un 72% de bits
imposibles, y hacen bien. Esto arregla los nombres, no las direcciones.

---

## Las tablas de xflags se localizan por forma

**Hecho el 14 ago 2026.** Era la última dirección cableada de las importantes,
y la que ya había roto una vez. Ahora `locate_xflag_tables()` las encuentra
sola y las constantes de `custom.h` se quedan **de contraste**: si lo que
aparece no está donde ellas dicen, se dice y se usa lo encontrado.

### Lo que hizo que fuera fácil

Antes de escribir nada, mirar la extra DMA entera de dos ROMs de familias
distintas. Ahí estaba todo:

```
dockiNAq (actual)                         Siixg4Kf (vieja)
0x080b0f00-0x080b0fca    202 B  raw       0x080948d0-0x0809499a    202 B  raw
0x080b0fd0-0x080b10ec    284 B  raw       0x080949a0-0x08094abc    284 B  raw
0x080b10f0-0x080b41c8  12504 B  raw       0x08094ac0-0x08097b98  12504 B  raw
0x080b41d0-0x080b42b4    228 B  raw       0x08097ba0-0x08097c84    228 B  raw
0x080b42c0-0x080b43ac    236 B  raw       0x08097c90-0x08097d7c    236 B  raw
0x080b43b0-0x080b6708   9048 B  raw       0x08097d80-0x0809a0d8   9048 B  raw
```

**Cada tabla es su propia entrada y va sin comprimir**, y los seis tamaños son
idénticos entre versiones: sólo se habían movido `0x1C630`. No había que
buscar dentro de ningún fichero.

De propina: `scenes` y `setups` son **byte a byte iguales** en las dos
familias. Lo único que cambia de contenido es `rooms`, que es dato que se lee.

### El criterio

Las tres tablas son una cadena, y una cadena se reconoce por su forma:

```
scenes[]  u16, no decreciente, empieza en 0, indexa setups[]
setups[]  u16, no decreciente, empieza en 0, indexa rooms[]
rooms[]   s16, el bit; sin orden ninguno
```

Un candidato son tres entradas seguidas, sin comprimir, donde las dos primeras
tienen esa forma y **cada una indexa dentro de la siguiente**:
`max(scenes) < len(setups)` y `max(setups) < len(rooms)`. Eso último es lo que
hace el criterio fuerte: no es «se parece», es que **la cadena cierra**.

Cuál es de cada juego sale de `scenes.yml`: los ids de OoT llegan a 100 y los
de MM a 113, así que la tabla tiene que ser lo bastante larga. El generador
emite la de OoT primero y así ha sido en las 29, pero el encaje se comprueba en
vez de darse por hecho.

### Medido sobre las 29 ROMs de `Downloads`

| | |
|---|---|
| cadenas encontradas | **exactamente 2 por ROM**, 0 falsos positivos |
| forma, en las 29 | OoT `101 / 142 / 6252`, MM `114 / 118 / 4524` |
| ROMs actuales (12) | devuelve **justo las constantes** `0x080B0F00` / `0x080B41D0` |
| ROMs viejas (17) | `0x080948D0` / `0x08097BA0` |

Que en las actuales devuelva las constantes es lo que convierte el cambio en un
**no-op comprobable**: `checks.json` sale byte a byte idéntico, y si no
saliera, el localizador estaría mal.

### La segunda barrera, que es la parte interesante

Con las tablas localizadas, la seed vieja dejó de abortar y pasó a resolver los
4.751 xflags... escribiendo un `checks.json` con **30 colisiones** (22 OoT, 8
MM), todas de `Boulder`. Los CSV del pool son de v32.0, esa versión tiene
actores que la vieja no, y sus filas caen sobre bits de otros checks.

O sea: **el cambio, tal cual, empeoraba las cosas.** Antes una seed vieja
abortaba y dejaba el `checks.json` bueno donde estaba; ahora lo pisaba con uno
en el que 30 checks se marcan entre ellos. Justo lo que dice la regla de que el
respaldo tiene que ser mejor que nada, no peor que lo que ya tenías.

Así que `collisions()` cuenta los pares que comparten bit sin ser vanilla/MQ
—que es la única coincidencia legítima— y aborta **antes de escribir**:

```
ABORTED: 30 pairs of checks share a bit without being
a vanilla/MQ pair. The tables were found, so the addresses are right;
what does not match this ROM is data/pool_*.csv, which is v32.0's.
checks.json is left untouched; whatever was there still stands.
   example: Lost Woods Rupee Arrow 1 / Lost Woods Boulder Early
```

Y el diagnóstico es preciso, que es lo que vale: **no dice «no puedo», dice qué
es lo que no cuadra**. Antes de esto, la misma seed daba «las tablas no
coinciden con las constantes», que ahora sería mentira.

El aviso de colisiones ya existía, pero se imprimía *después* de escribir el
fichero y sólo como una línea más entre cuarenta. Existir no es lo mismo que
frenar.

### Cabo suelto

Los **CSV del pool** son ahora la dependencia de versión visible. Hasta que las
filas salgan de la ROM o se elija el CSV por versión, las seeds viejas se
pararán en esa segunda barrera, que es lo correcto.

Y de paso: una ROM que no sea de OoTMM daba un `struct.error` sobre tamaños de
buffer. Ahora `rom.extra_dma()` lo comprueba una vez y dice
`it does not look like an OoTMM seed`.
