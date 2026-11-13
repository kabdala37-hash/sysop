# sysop-agent

Agente observador de servidores y de las plataformas que alojan. Determina qué
debería estar funcionando, lo contrasta con el estado real, y cuando algo no
coincide abre un incidente con evidencia, causa probable y próxima acción segura.

**Solo observa y diagnostica. No ejecuta acciones sobre lo que observa.**

**[`ARQUITECTURA.md`](ARQUITECTURA.md)** explica en detalle qué hace, cómo está
construido, qué usa, cómo se configura y qué tiene adentro, con diagramas. Es el
documento para leer antes de tocar el código.

Especificación de origen y plan de implementación en el repo de la plataforma,
bajo `.docs/Agente Observador/`.

## Estado

En marcha en srv3. **39 controles de los 40 de la v1**: los 14 del sistema
operativo y 25 de los 26 de la aplicación. El que falta es DB-13 (respaldo
dentro del objetivo de recuperación), bloqueado hasta saber dónde están los
respaldos de producción.

Motor completo, correo por los dos canales y diagnóstico con modelo. Lo que
sigue es el despliegue a producción.

## Por qué existe separado de `plataforma`

Para poder actualizarse sin pasar por el `git pull` de la plataforma. El
observador tiene que seguir en pie justamente cuando el despliegue de la
plataforma sale mal, así que no puede compartir su mecanismo de despliegue.

## Estructura

```
sysop/
├── core/                    motor: umbrales, histéresis, incidentes, fuentes
├── adapters/
│   ├── so_linux/            el sistema operativo. Igual en todos lados
│   └── apps/                una aplicación por carpeta (hoy: plataforma)
├── store/                   almacén local SQLite/WAL
├── notify/                  armado y entrega del correo
├── llm/                     gateway, contexto redactado y esquema estricto
├── diagnostico.py           cuándo se pregunta y qué se hace con la respuesta
├── daemon.py                el servicio: controles y alertas urgentes
├── informe.py               el informe periódico, disparado por cron
└── cli.py                   estado, informe, probar
config/
├── so.yaml                  lo del sistema operativo
├── app.yaml                 lo de la aplicación, incluidas las fuentes
└── hosts/<host>.yaml        lo propio de cada servidor
install/                     unidad systemd, cron del informe y SQL
```

### Las dos mitades

El **adaptador de sistema operativo** no sabe qué aplicación corre encima. El
**adaptador de aplicación** sabe de una plataforma y sólo de una. El núcleo no
sabe de ninguno de los dos: recibe hechos con la misma forma y decide qué hacer.

Agregar una aplicación es escribir un módulo en `adapters/apps/` y un `app.yaml`.
No se toca el núcleo ni el adaptador de sistema operativo.

## Cómo se escriben los logs que este servicio lee

**[`CONVENCION-DE-LOGS.md`](CONVENCION-DE-LOGS.md)** es la especificación: el
formato, los niveles, las claves de contexto que se repiten entre aplicaciones,
qué no se loguea nunca y con qué rotación.

No es una librería: no hay nada que importar. Cada aplicación implementa su
logger respetando ese formato, y el documento trae la implementación de
referencia en Python y en Node para copiar. **Al arrancar un programa nuevo,
pasarle ese archivo alcanza.**

El observador tolera los formatos viejos, así que la convención mejora la
precisión sin ser un requisito. Pero cada componente que se adapta es un
componente cuyos errores se pueden **contar** en vez de sólo leer.

## Tres reglas del diseño

**El plano determinístico decide, el modelo explica.** Ningún estado lo fija un
modelo de lenguaje. El modelo correlaciona y redacta sobre evidencia ya
comparada; nunca declara una caída ni cambia una expectativa.

Lo que sí puede hacer es marcar un incidente como falso positivo, y para eso
tiene tres límites: sólo con confianza **alta**, nunca sobre un control crítico
o de la lista protegida, y **descartar no es cerrar** — el incidente sigue
abierto, sigue midiéndose y sigue apareciendo en el informe, en su propia
sección.

**La ausencia de evidencia produce `UNKNOWN`, no `OK`.** Está impuesto en el
código: un `CheckOutcome` con estado `OK` y `evidence_quality=NONE` lanza
excepción. Un observador que confunde "no miré" con "está bien" es peor que no
tener observador.

**El adaptador consume la lógica de su plataforma, no la reimplementa.** La
derivación de workers, la elegibilidad del pipeline y los contratos de los
trabajos se piden al back-end por endpoints internos. Una copia diverge el día
que alguien toca el original, y a partir de ahí el observador miente sin que
nadie se entere.

## Instalación

```bash
python3 -m venv venv && ./venv/bin/pip install -e .
```

En un servidor, además:

1. `install/sql/001_monitor_user.sql` — usuario MySQL de monitoreo.
   Necesita `PROCESS` y `SELECT` sobre `performance_schema`: sin eso, cinco
   controles de base de datos son imposibles. Verificado, no supuesto.
2. `install/sql/002_email_report_scope.sql` — agrega el scope `sysop` y las
   columnas de horario a la pantalla de destinatarios del front.
3. `install/sql/003_permissions.sql` — los dos permisos.
4. `install/sysop-agent.service` — la unidad. Usuario `sysop-agent` sin shell,
   `ProtectSystem=strict` y escritura limitada a sus directorios.
5. `install/sysop-agent.cron` — el informe periódico. **La cadencia vive en ese
   archivo y en ningún otro lado**: pasar de diario a semanal es editar una línea.

A diferencia de las migraciones de la plataforma, **este SQL sí va a git.** Que
haya que pasar cada ALTER a mano es un problema conocido; no lo heredamos.

### Rutas

```
/opt/sysop-agent/       binarios          root:root       0755
/etc/sysop-agent/       config y secretos root:sysop-agent 0640
/var/lib/sysop-agent/   almacén local     sysop-agent      0750
/var/log/sysop-agent/   auditoría         sysop-agent      0750
```

`PrivateTmp=no` en la unidad es deliberado: los pidfiles de la plataforma viven
en `/tmp` y hay que leerlos para saber si un trabajo está corriendo de verdad.

### Sobrecarga

Medido en srv3 con 79 controles: **0,79% de un core y 36 MiB**, sobre una ventana
de cinco minutos. El criterio es 1% y 150 MiB, así que el margen de CPU no es
cómodo: conviene medirlo en cada servidor nuevo. Cómo, y dónde se va el tiempo,
en `install/RUNBOOK.md`.

## Verificación de los bloqueos

El control de trabajo abandonado **no intenta tomar el bloqueo.** Consulta la
tabla del kernel:

```bash
stat -c '%d %i' /tmp/plataforma_<name>_<scope>.pid
grep FLOCK /proc/locks
```

Intentar tomarlo, como sugiere la especificación original, se lo robaría a un
trabajo que arranque en ese instante y lo mataría. El observador provocaría la
falla que dice observar.

## Los dos canales de salida

| Canal | Quién lo dispara | Cuándo |
|---|---|---|
| Informe periódico | `/etc/cron.d/sysop-agent` | siempre, aunque esté todo bien |
| Alerta urgente | el daemon, en el momento | sólo severidad crítica, pasada la histéresis |

El informe lo dispara cron y no el daemon a propósito: **si el daemon se muere,
el trabajo corre igual y manda el informe diciendo que está muerto.** Un informe
que no llega es indistinguible de un "todo bien", y eso hace de latido sin
necesidad de ningún proceso vigilando desde otro servidor.

## Ver el estado

No hay panel web ni puerto abierto. Se mira desde el servidor:

```bash
sysop-agent estado         # qué ve el observador ahora
sysop-agent probar         # corre un ciclo completo sin mandar nada
sysop-agent informe        # arma y manda el informe (lo llama cron)
sysop-agent diagnosticar   # le pide al modelo que explique lo que hay abierto
```

## El informe: qué pasó, no cómo está

El informe periódico cubre **desde el informe anterior hasta ahora**. Esa ventana
sale de la bandeja de salida, no de un número fijo: cambiar la cadencia del cron
a semanal cambia el período solo.

Cuatro bloques, y los dos del medio son los que no se ven de ninguna otra forma:

| Bloque | Qué responde |
|---|---|
| Incidentes | qué abrió, qué se resolvió, qué sigue abierto y cuánto duró |
| **Transitorios** | fallas que se recuperaron **sin llegar a abrir incidente** |
| **Ceguera** | qué controles quedaron sin poder ver nada, y por qué |
| Actividad | cuánto se midió, para poder dimensionar el resto |

Un servicio que se cae una sola muestra a las tres de la mañana y vuelve solo no
abre incidente —y está bien que no lo abra— pero que haya pasado diecisiete
veces en la semana es un dato. Eso son los transitorios.

## El diagnóstico

El agente sale de la plataforma (`agents`, con su credencial), no del código:
el prompt y el modelo se editan desde el front sin desplegar nada. En una
plataforma sin tabla de agentes, `fuentes.agente: {tipo: archivo}` y el prompt
en `/etc/sysop-agent/agente.md`.

Un solo agente y un solo modelo para los dos casos: el modo (`urgente` o
`periodico`) va en el paquete, no en la identidad. Una credencial, un prompt.

En modo urgente sólo se llama cuando **se abre** un incidente, así que un día
sano no gasta nada. En modo periódico se llama siempre, una vez por informe: un
"no pasó nada grave, pero estos tres se repiten" es justamente lo que no se ve
de otra forma.

### Puede ir a buscar

Cuando la evidencia no alcanza, el agente tiene cuatro herramientas de
**lectura**: `listar`, `leer` (con `buscar`), `telemetria` y `journal`. Puede
moverse por donde necesite; lo que lo orienta no es una lista de rutas
permitidas sino el bloque `mapa:` del YAML del host, que dice qué es cada ruta,
qué va a encontrar y cuándo conviene mirarla.

Dos límites, y ninguno restringe dónde puede mirar: los archivos que son puro
credencial no se leen (viajarían al proveedor del modelo y ningún diagnóstico
los necesita), y hay techo de vueltas para que una pregunta no se vaya en
consultas.

**El mapa define el alcance real de la exploración, aunque no restrinja nada.**
Probado: se le dio un servicio caído cuya causa estaba en
`/var/log/facturacion-nz/procesador.log`, una ruta que no figuraba en ningún
lado. Con el mapa listando sólo `/var/log/plataforma`, el agente buscó ahí,
no encontró nada y se rindió con confianza media. Agregando `/var/log` al mapa
como raíz —con la nota de que ahí están **todos** los logs, no sólo los
nuestros— listó el directorio, encontró la carpeta del servicio, leyó el archivo
y dio la causa exacta con confianza alta.

La lección para escribir el mapa de un servidor nuevo: **declarar las raíces, no
sólo lo conocido**. Lo que no está en el mapa, para el agente no existe. Medido en srv3: unos 1.700 tokens de entrada y 120 de salida por
diagnóstico, alrededor de 0,0017 USD y 4 segundos.

Tres cosas están antes de la primera llamada, no después: presupuesto diario
duro (agotado, el observador avisa igual, sin diagnóstico), corte a los tres
fallos seguidos, y un tope de intentos por incidente para que uno que el modelo
no sabe contestar no bloquee la cola ni gaste tokens para siempre.

Lo que se le manda está redactado: nunca credenciales, ni teléfonos o correos de
clientes finales. La identidad interna sí va —el nombre de un servicio es lo que
hace útil el diagnóstico— y las marcas de tiempo también, que son lo que
distingue un pico de una tendencia.
