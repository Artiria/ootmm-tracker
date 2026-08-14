# Código y datos de terceros

Este tracker es código original, pero **se apoya en ficheros del repositorio
de OoTMM**, que viajan dentro de `data/` y también dentro del `.exe`. OoTMM
está bajo licencia MIT, que permite copiarlos, modificarlos y redistribuirlos
con una sola condición: conservar su aviso de copyright y el texto de la
licencia. Eso es lo que hace este fichero.

Nada de lo que hay aquí es propiedad intelectual de Nintendo: el tracker **no
reparte ni un sprite ni un byte de los juegos**. Los iconos los extrae cada
usuario de su propia ROM al arrancar el programa por primera vez (ver la
sección «Repartir el tracker» del README).

## OoTMM

- Repositorio: <https://github.com/OoTMM/OoTMM>
- Licencia: MIT
- Copyright (c) 2020-2022 OoTMM Team

### Ficheros que vienen de allí

| Fichero en este repo | Origen en OoTMM/OoTMM | Para qué se usa |
|---|---|---|
| `data/pool_oot.csv` | `data/pool/pool_oot.csv` | diccionario de etiquetas del pool (`mkchecks.py`) |
| `data/pool_mm.csv` | `data/pool/pool_mm.csv` | ídem |
| `data/scenes.yml` | `data/defs/scenes.yml` | nombre de escena -> índice |
| `data/npc.yml` | `data/defs/npc.yml` | símbolo de npc -> índice |
| `data/gi.yml` | `data/defs/gi.yml` | id simbólico de la tabla GI (`placement.py`) |
| `data/ref/items.h` | `packages/generator/include/combo/data/items.h` | ids de item, parseado en ejecución |
| `data/ref/mark.c` | `packages/generator/src/common/mark.c` | referencia del formato de marcas |
| `data/ref/xflags.c` | `packages/generator/src/common/xflags.c` | referencia del formato de xflags |

Son copias literales, sin modificar. Comprobado contra `master` el 14 de
agosto de 2026: siete de los ocho son idénticos línea a línea; `gi.yml` es una
copia de una versión anterior y difiere en 26 líneas, todas cambios de nombre
que OoTMM hizo después (las etiquetas de las Rusty Key). No afecta: los
nombres de item los lee el tracker de la ROM, y de `gi.yml` sólo usa el
símbolo.

`data/ref/mark.c` y `data/ref/xflags.c` no se leen en ejecución; están
guardados porque son la documentación del formato que este tracker
descodifica, y conviene tenerlos fijados a una versión concreta.

### Licencia de OoTMM, íntegra

```
MIT License

Copyright (c) 2020-2022 OoTMM Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Lo que no se reparte

`adapter-tracker.lua`, que usa el modo `proxy`, es copia literal del
`adapter.lua` del MultiClient de OoTMM con el puerto cambiado. **No está en
este repositorio** y no se distribuye: quien quiera ese modo lo hace a partir
del suyo.
