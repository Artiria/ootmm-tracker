# Autotracker OoTMM

Lee el estado de una partida de OoTMM desde Project64-EM y lo traduce a
nombres: inventario, canciones, máscaras, equipo, mejoras y checks.

El **cómo y el porqué** están en [`ootmm-autotracker-poc.md`](ootmm-autotracker-poc.md):
direcciones, offsets, qué se verificó y cómo, y los cabos sueltos. Léelo antes
de tocar nada.

## Arrancar

### Con el `.exe` (no hace falta Python)

Doble clic en `ootmm-tracker.exe` y ya: detecta la ROM, genera sus tablas e
iconos la primera vez, deja `tracker.lua` en la carpeta `Scripts\` del
emulador y abre el overlay. En el emulador, con la ROM cargada:
**Debugger > Scripts**, ejecutar `tracker.lua`. El orden da igual.

Los subcomandos de abajo funcionan igual: `ootmm-tracker.exe items`.

Lo que genera **no** va junto al ejecutable, va a
`%LOCALAPPDATA%\OoTMM-Tracker\` (`checks.json`, `icons.*`, la caché, y la
carpeta `icons\` donde poner los tuyos).

> Windows Defender y compañía desconfían de los ejecutables de PyInstaller sin
> firmar. No está firmado —firmar cuesta dinero— así que puede saltar un aviso
> de SmartScreen la primera vez. Quien prefiera no fiarse tiene el código aquí
> al lado, que hace exactamente lo mismo.

### En multiworld (sin probar todavía)

Cada jugador lleva **su** ROM, **su** partida y **su** tracker: el `.exe` de
cada uno detecta lo suyo y no habla con el del otro. El tracker ya entiende que
un item sea de otro mundo — lo lee de la tabla de la ROM y lo apunta como
`player`.

**Pero antes de sentaros a una partida larga, haced esta prueba de dos
minutos**, porque decide si esto funciona y nunca se ha comprobado:

> Con la ROM cargada, abrid **Debugger > Scripts** y arrancad el `adapter.lua`
> del MultiClient **y** `tracker.lua`, en ese orden. Mirad si los dos siguen
> vivos y si el overlay se pone en `ready`.

- **Si aguantan los dos**: listo, no hay que hacer nada más. El tracker va
  igual que en single.
- **Si el emulador sólo deja uno**: entonces **hoy no se puede** llevar el
  tracker y el multi a la vez, y la partida hay que jugarla sin tracker. No
  hay apaño: el modo `proxy` **no vale** para esto —es una herramienta de
  diagnóstico, se pone en medio para apuntar qué direcciones usa el multi y
  escupir un resumen, pero no alimenta el overlay—. Haría falta un script Lua
  que hiciera las dos cosas, y no está escrito.

Sea cual sea el resultado, **anotad qué dijo el emulador al intentarlo**: es la
pregunta P4 del POC, lleva abierta desde el principio del proyecto, y esa
prueba de dos minutos la cierra.

### Desde el código

1. Copia `Scripts\tracker.lua` a la carpeta `Scripts\` de Project64-EM (o
   `python ootmm.py install-lua`, que la busca sola). Con la ROM cargada:
   **Debugger > Scripts**, ejecutar `tracker.lua`.
2. Aquí:

```
python ootmm.py items
```

Localiza los saves por firma, calibra el ruido 6 segundos (no toques nada) y a
partir de ahí canta cada cambio. Aguanta el cambio entre OoT y MM: cuando
cruzas, relocaliza las bases solo.

El orden da igual — `tracker.lua` reintenta la conexión hasta que levantas el
daemon, y reconecta si lo reinicias.

## Subcomandos

| | |
|---|---|
| `items` | inventario de ambos juegos en bucle, cantando cambios |
| `checks` | checks completados, resueltos a nombre del spoiler |
| `watch ADDR:SIZE,…` | sondear direcciones sueltas |
| `dump ADDR:LEN` | volcar una región (acepta `oot`, `mm`) |
| `find fichero PATRÓN` | buscar una firma en un volcado |
| `diff a b` | comparar volcados |
| `proxy` | capturar qué direcciones usa el MultiClient |
| `install-lua` | copiar `tracker.lua` a la carpeta del emulador |

Para ver los checks con el item de cada uno:

```
python ootmm.py checks --spoiler C:\...\OoTMM-f5PCTnhD\OoTMM-Spoiler-f5PCTnhD.txt
```

**El overlay no necesita spoiler**: qué item hay en cada sitio lo lee de la
propia ROM (`placement.py`, tabla `COMBO_VROM_CHECKS`), que es de donde lo saca
el juego. Con eso funcionan el filtro de relleno y los pendientes con su item,
sin fichero que buscar ni cargar.

Los **nombres** también salen de la ROM, de `kItemNames[]` en el payload. Así
no dependen de que `data/gi.yml` sea de la misma versión de OoTMM que la seed:
ese fichero se indexa por posición, y con una seed vieja los nombres salían
corridos sin que nada avisara. Se queda sólo para el símbolo (`OOT_BOMBS_5`),
y únicamente si sus nombres siguen coincidiendo con los de la ROM.

El botón **Cargar spoiler…** de la vista de director se queda como respaldo,
para cuando de una ROM no se pueda leer esa tabla. Comprueba versión,
coincidencia de nombres y cobertura antes de aceptarlo, y lo que cargues se
pone por encima de lo leído de la ROM.

## Ficheros

| | |
|---|---|
| `ootmm.py` | la herramienta |
| `paths.py` | qué viaja con el programa y qué genera (importa dentro del `.exe`) |
| `ootmm.spec` | receta de PyInstaller: `python -m PyInstaller ootmm.spec` |
| `Scripts/tracker.lua` | el servidor de memoria que corre dentro del emulador |
| `inventory.py` | mapa del inventario de los dos juegos y tabla de ids |
| `mkchecks.py` | genera `checks.json` desde `data/` y la ROM |
| `placement.py` | qué item hay en cada sitio y cómo se llama, leído de la ROM (sustituye al spoiler) |
| `mkicons.py` | extrae los iconos de la ROM |
| `discover.py` | detecta ROM, spoiler y regenera lo que haga falta |
| `overlay.py` / `overlay.html` | el tracker mirable |
| `rom.py` | leer la ROM: Yaz0, dmadata, extra DMA |
| `fakelua.py` | un `tracker.lua` de mentira que sirve un volcado: probar sin emulador |
| `checks.json` | 6043 ubicaciones; 5981 con dirección resuelta |
| `data/` | datos del repo de OoTMM (pool, escenas, npc) |
| `data/ref/` | fuentes en que se apoya el mapeo: `mark.c`, `xflags.c`, `items.h` |
| `LICENSE` | licencia MIT de este proyecto |
| `THIRD-PARTY.md` | qué ficheros vienen de OoTMM y bajo qué licencia |

### Volcados de referencia

Sirven para probar cambios **sin arrancar el emulador**, y son los dos layouts
de RAM posibles:

```
ram-en-oot.bin      jugando OoT:  save OoT 0x8011A5D0 · MM 0x8044BE18
ram-en-mm.bin       jugando MM:   save MM  0x801EF678 · OoT 0x8076C4F0
fla-deswapeado.bin  el fichero .fla con las palabras ya invertidas
```

Que las bases cambien al cruzar de juego es el gotcha más peligroso del
proyecto: con offsets fijos el tracker funciona en OoT y lee basura en MM sin
dar ningún error. Por eso se localizan por firma y por eso conviene probar
contra los dos volcados.

```
python ootmm.py items --dump ram-en-oot.bin
python ootmm.py checks --dump ram-en-oot.bin
```

Y el overlay entero, que no tiene `--dump`, con el Lua de mentira: en una
consola el tracker (o el `.exe`), en otra el volcado.

```
python ootmm.py overlay --no-window --port 13261
python fakelua.py ram-en-mm.bin 13261
```

## Repartir el tracker

**Se reparte código, nunca arte ni datos de partida.** Cada uno genera lo suyo
a partir de su propia copia del juego, y eso deja el paquete limpio de
material de Nintendo.

Lo que se reparte:

```
ootmm.py  overlay.py  overlay.html  mkchecks.py  mkicons.py  discover.py
inventory.py  placement.py  rom.py  paths.py  fakelua.py  data/
Scripts/tracker.lua  ootmm.spec  README.md  LICENSE  THIRD-PARTY.md
```

O el `.exe`, que lleva todo eso dentro y **tampoco** lleva arte ni datos de
partida: se construye con `python -m PyInstaller ootmm.spec` y sale
`dist/ootmm-tracker.exe`, 8,9 MB.

Licencia **MIT** (ver [`LICENSE`](LICENSE)), la misma que OoTMM: se puede
descargar, usar, modificar y redistribuir sin pedir permiso. Los ficheros de
terceros que van dentro están declarados en
[`THIRD-PARTY.md`](THIRD-PARTY.md).

`adapter-tracker.lua`, que usa el modo `proxy`, **no se reparte**: es copia
literal del `adapter.lua` del MultiClient con el puerto cambiado, o sea código
de otro. Quien quiera usar ese modo lo hace él a partir del suyo.

Lo que **no** se reparte, porque se genera solo en cada máquina (y está en
`.gitignore`):

| Fichero | Qué es | De dónde sale |
|---|---|---|
| `icons.png` / `icons.json` | los iconos de items | `mkicons.py`, extraídos de **su** ROM |
| `checks.json` | las 6.043 ubicaciones | `mkchecks.py`, de **su** ROM y su spoiler |
| `discover-cache.json` | rutas y hashes | su emulador |
| `icons/*` | imágenes que cada uno ponga | las pone él |

Quien lo instale sólo tiene que abrir el `.exe` (o lanzar
`python ootmm.py overlay`): detecta su ROM sola —por el hash de la carpeta de partidas de Project64—, busca su
spoiler al lado, y genera iconos y tablas en su máquina la primera vez. No
hay paso manual ni hay que pasarle rutas.

> Los iconos salen de la ROM de cada uno, **los de los dos juegos**. Los de
> OoT están en `icon_item_static`; los de MM en un archivo CmpDma, con cada
> icono comprimido aparte, y de ahí salen las 24 máscaras. Quien quiera
> sustituir alguno por otra imagen puede ponerla en `icons/`
> (ver [`icons/LEEME.md`](icons/LEEME.md)); eso no viaja en el paquete.

## Apoyar

El tracker es gratis y lo seguirá siendo. Todo lo que hace está en este
repositorio y **nada queda detrás de un pago**: no hay versión de pago, ni
funciones reservadas, ni claves.

Si te ha resultado útil y te apetece invitar a algo, hay un botón de
patrocinio en la página del repositorio. Que quede claro de qué es la
donación: **es por el tracker**, que es código propio y no reparte nada de los
juegos. Ni el randomizer, ni las ROMs, ni el trabajo de OoTMM tienen nada que
ver con ella.

## Créditos

Esto no existiría sin **[OoTMM](https://github.com/OoTMM/OoTMM)**, el
randomizer que combina Ocarina of Time y Majora's Mask, ni sin su equipo. El
tracker lee las estructuras que ellos inventaron —los xflags, la tabla de
checks, la extra DMA del payload— y se apoya en varios ficheros de datos de su
repositorio, listados en [`THIRD-PARTY.md`](THIRD-PARTY.md).

Gracias también a la gente del Discord de OoTMM, donde se resuelven las dudas
de formato que no están escritas en ningún sitio.

## Licencia

MIT — ver [`LICENSE`](LICENSE). Lo de terceros, en
[`THIRD-PARTY.md`](THIRD-PARTY.md).

## Siguiente

Por orden de valor:

1. Confirmar en juego el `perm` de MM y el `gsFlags` de OoT.
2. Los 80 checks que faltan: `caughtFishFlags`, stray fairies de MM, `cow`.
3. La parte multi: dos scripts Lua a la vez, y el buzón coop.
