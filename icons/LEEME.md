# Iconos puestos a mano

Todo lo que dejes aquí manda sobre el icono sacado de la ROM.

**Ya no hace falta para las máscaras de Majora**: se extraen de tu propia ROM
y salen con su arte de verdad. Esta carpeta queda para gustos — sustituir un
icono por otro que te guste más, o poner algo donde el juego no tenga nada.

No se descarga nada. Las imágenes las pones tú, de donde tú decidas.

## Dónde va cada cosa

```
icons/mm/deku-mask.png      solo para Majora's Mask
icons/oot/kokiri-sword.png  solo para Ocarina of Time
icons/algo.png              vale para los dos
```

## Cómo se llama el fichero

El nombre se compara **normalizado** —minúsculas, y todo lo que no sea letra
o número pasa a guion—, así que valen dos formas y no hay que acertar con
mayúsculas ni apóstrofes:

| Vale | Porque |
|---|---|
| `deku-mask.png` | es el nombre que enseña el overlay al pasar el ratón |
| `mask-deku.png` | es el nombre de `items.h` para ese id |
| `Deku Mask.png` | se normaliza igual que el primero |
| `garos-mask.png` | «Garo's Mask» sin el apóstrofe |

Si el hueco está ocupado se prueban los dos nombres; si está vacío, sólo el
primero. Primero se mira la carpeta del juego y luego la común.

Formatos: `.png`, `.gif`, `.webp`, `.jpg`.

## Detalles prácticos

- **No hace falta reiniciar**: el overlay vuelve a leer la carpeta cuando
  cambia, así que sueltas la imagen y aparece en el siguiente refresco.
- Cuadradas y con fondo transparente quedan mejor; se escalan solas al tamaño
  de la celda.
- Se pintan en gris y hundidas cuando no tienes el objeto, y a color cuando
  sí, igual que los iconos de la ROM.
- El servidor sólo sirve ficheros que estaban al escanear la carpeta, así que
  no se puede pedir nada de fuera de aquí por URL.

## Cómo saber el nombre

Pasa el ratón por encima de cualquier celda del overlay: sale el nombre
completo, y ése es el que hay que usar de fichero.
