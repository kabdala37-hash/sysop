# sysop-agent — qué es, cómo funciona y cómo se configura

Documento de referencia del agente observador. Escrito el 2026-08-28.

Para instalarlo, ver `install/RUNBOOK.md`. Para escribir logs que este servicio
pueda leer bien, ver `CONVENCION-DE-LOGS.md`.

---

## 1. Qué hace

Un programa que se instala en un servidor, mira ese servidor, y **manda un mail
cuando algo está mal**.

Nada más que eso, y el "nada más" es deliberado: no hay nodo central, no hay
componentes que se hablen entre servidores, no hay panel web. Si mañana hay diez
servidores, hay diez instalaciones iguales e independientes, cada una mandando
su propio correo.

Lo que lo distingue de un monitor común son tres cosas:

**Sabe qué debería estar pasando, no sólo qué está pasando.** No mira "hay
procesos corriendo", mira "faltan tres de los siete workers que la configuración
dice que deberían existir". La diferencia es que lo segundo se puede detectar y
lo primero no.

**Verifica resultados, no ejecuciones.** Un trabajo programado puede lanzarse
puntual, salir con código cero y no haber hecho nada. Hay un control dedicado a
esa forma de fallar, que es la más silenciosa de todas.

**Nunca confunde "no lo pude mirar" con "está bien".** Es la regla que atraviesa
todo el diseño, y está impuesta en el código: un resultado `OK` sin evidencia
lanza excepción.

---

## 2. El recorrido completo

Desde que se mide algo hasta que llega un correo:

```
    ┌──────────────────────────────────────────────────────────────────────┐
    │  1. MEDIR                        cada 15 s, o lo que diga el control │
    └──────────────────────────────────────────────────────────────────────┘
         adaptador SO ─────┐
                           ├──▶  hechos con la misma forma:
         adaptador app ────┘      (control, recurso, esperado, medido, ¿pude leer?)
                                            │
                                            ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │  2. DECIDIR SI ES UN PROBLEMA                          histéresis    │
    └──────────────────────────────────────────────────────────────────────┘
         ¿falló?  ── no ──▶  se guarda el resultado y listo
             │
            sí
             ▼
         ¿falló las N veces seguidas que pide el control?
             │                                    │
            no                                   sí
             │                                    │
             ▼                                    ▼
      es un pico: queda registrado          se abre un INCIDENTE
      y aparece en el informe como
      "transitorio", sin avisar                   │
                                                  ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │  3. ENTENDER QUÉ PASA                              el modelo opina   │
    └──────────────────────────────────────────────────────────────────────┘
         se arma un paquete de evidencia y se le pregunta al agente.
         El agente puede pedir más datos por su cuenta: leer un log,
         listar un directorio, mirar la telemetría de ahora.
                                                  │
                            ┌─────────────────────┴─────────────────────┐
                            ▼                                           ▼
                    "es real"                                  "es falso positivo"
                            │                                           │
                            │                              ¿es un control crítico?
                            │                                    │           │
                            │                                   sí          no
                            │                                    │           │
                            ▼                                    ▼           ▼
    ┌──────────────────────────────────────────┐          avisa igual   se marca y
    │  4. AVISAR                               │          (el modelo    no cuenta
    └──────────────────────────────────────────┘           no puede      como
         ¿es crítico?                                      apagar un     problema
             │                │                            aviso)        activo
            sí               no                                          (sigue
             │                │                                          abierto)
             ▼                ▼
      ALERTA URGENTE     espera al informe
      ahora mismo        periódico
```

Los dos caminos del final son los únicos dos correos que este servicio manda.

---

## 3. Cómo está construido

Tres piezas, y la separación entre ellas es lo que permite que el mismo programa
sirva para otra aplicación mañana.

```
                        ┌───────────────────────────────────┐
                        │             NÚCLEO                │
                        │                                   │
                        │  no sabe de sistemas operativos   │
                        │  ni de aplicaciones               │
                        │                                   │
                        │  · motor y presupuesto de ciclo   │
                        │  · umbrales e histéresis          │
                        │  · incidentes y su ciclo de vida  │
                        │  · resumen del período            │
                        │  · diagnóstico con modelo         │
                        │  · correo                         │
                        └───────┬───────────────────┬───────┘
                                │                   │
                     hechos     │                   │    hechos
                                │                   │
              ┌─────────────────┴─────┐   ┌─────────┴──────────────────┐
              │   ADAPTADOR DE SO     │   │   ADAPTADOR DE APLICACIÓN  │
              │   so_linux            │   │   apps/plataforma      │
              │                       │   │                            │
              │   FIJO: igual en      │   │   ENCHUFABLE: uno por      │
              │   todos los servidores│   │   plataforma               │
              │                       │   │                            │
              │   · procesos y RAM    │   │   · trabajos programados   │
              │   · discos e inodos   │   │   · workers de voz         │
              │   · unidades systemd  │   │   · colas y pipelines      │
              │   · journal del kernel│   │   · MySQL                  │
              │   · puertos y TLS     │   │   · costo de modelos       │
              │                       │   │   · de dónde sale el       │
              │   14 controles        │   │     agente y a quién avisar│
              │                       │   │   25 controles             │
              └───────────────────────┘   └────────────────────────────┘
                        │                              │
                        ▼                              ▼
                 /proc, systemd,              MySQL, endpoints internos
                 journalctl, netlink          del back, /proc/locks
```

**El adaptador de sistema operativo no sabe qué aplicación corre encima.** Esa
ignorancia es lo que lo hace reutilizable: instalarlo en un servidor de otro
producto no pide tocarle una línea.

**El adaptador de aplicación sabe de una plataforma y sólo de una.** Agregar otra
es escribir un módulo nuevo en `adapters/apps/` y un `app.yaml`. No se toca el
núcleo ni el adaptador de sistema operativo.

**La dependencia va en un solo sentido.** El adaptador de aplicación puede leer
del de sistema operativo —`cron.service` es infraestructura del SO de la que la
plataforma depende— pero nunca al revés. Verificado: ni el núcleo ni `so_linux`
importan nada de `apps/`.

### Lo que tiene que cumplir un adaptador de aplicación

```
   descubrir()          qué recursos existen (trabajos, workers, colas)
   esperado()           qué debería existir, según la fuente de verdad
   controles()          qué controles aporta
   proveer()            qué recursos comparte durante un ciclo
   tipos_de_recurso()   qué tipos administra
   ─────────────────────────────────────────────────────────────────
   obtener_agente()     de dónde sale el agente que diagnostica
   destinatarios()      a quién se le manda el correo
```

Las dos últimas son las que hacen que el núcleo no sepa de dónde vienen las
cosas: todo lo que antes leía de la base de la plataforma ahora se lo pide al
adaptador.

**Un adaptador no reimplementa la lógica de su plataforma: la consume.** La
derivación de qué workers deberían existir se le pide al back-end por un
endpoint interno, porque es la misma función que usa el supervisor de líneas
para decidir qué levantar. Si el observador la copiara, el día que alguien toque
esa función las dos versiones divergirían en silencio y el observador empezaría
a mentir.

---

## 4. El ciclo de vida de un incidente

Es la máquina que evita que el correo sea ruido. Cada transición existe por una
forma concreta de fallar.

```
                          el control falla
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  ¿falló `abre_tras`    │
                    │   veces seguidas,      │──── no ──▶  TRANSITORIO
                    │   con el mismo motivo? │             (no abre nada;
                    └────────────┬───────────┘              se cuenta y sale
                                sí                          en el informe)
                                 │
                                 ▼
                    ╔════════════════════════╗
                    ║        OPEN            ║ ◀──────────────┐
                    ╚════════════┬═══════════╝                │
                                 │                            │
                    el control vuelve a estar sano            │
                    `cierra_tras` veces                       │
                                 │                     vuelve a fallar
                                 ▼                     antes de cerrar
                    ╔════════════════════════╗                │
                    ║      RECOVERED         ║ ───────────────┘
                    ╚════════════┬═══════════╝
                                 │
                    pasan 15 minutos sin volver
                                 │
                                 ▼
                    ╔════════════════════════╗
                    ║       CLOSED           ║
                    ╚════════════════════════╝
                          puede reabrir con
                          el mismo fingerprint
```

**Por qué hay un estado intermedio.** Si un incidente cerrara apenas el síntoma
desaparece, un problema intermitente produciría una serie de incidentes cortos
que parecen resolverse solos, escondiendo que en realidad nunca se arregló.

**Cómo se identifica un incidente.** Por un *fingerprint* de
`(control, recurso, motivo)`. Incluye el motivo y no sólo el control porque un
mount al 92% y ese mismo mount montado de sólo lectura son dos problemas
distintos sobre el mismo recurso; colapsarlos haría que el segundo quede tapado
por el primero. No incluye la severidad: un incidente que escala de `major` a
`critical` es el mismo incidente empeorando.

**La histéresis es asimétrica a propósito:** abre rápido y cierra lento. Abrir
tarde pierde tiempo de reacción; cerrar temprano hace que un problema
intermitente se reporte como recuperado entre pico y pico.

**Un `UNKNOWN` no mueve nada.** No abre incidentes —una fuente caída generaría
uno por cada recurso que dependa de ella— y tampoco los cierra, porque un
problema real no se resuelve por el hecho de que se haya caído el collector que
lo estaba viendo.

---

## 5. El diagnóstico con modelo

Un solo agente, un solo modelo, un solo prompt. Lo que cambia entre un incidente
urgente y el informe del período es el paquete que recibe, no quién responde.

```
    ┌─────────────────────────────────────────────────────────────────────┐
    │  EL PAQUETE                                       ya redactado      │
    ├─────────────────────────────────────────────────────────────────────┤
    │  modo                 "urgente" | "periodico"                       │
    │  instalacion          srv2 | srv3                                     │
    │  mapa_del_servidor    qué hay en cada ruta y cuándo mirarla         │
    │                                                                     │
    │  · en modo urgente:                                                 │
    │      incidente        control, recurso, esperado, medido            │
    │      muestras         las últimas 12 del mismo control              │
    │      vecinos          qué más estaba roto al mismo tiempo           │
    │      eventos          la línea temporal del incidente               │
    │                                                                     │
    │  · en modo periódico:                                               │
    │      incidentes       abiertos, cerrados y vivos, con duración      │
    │      transitorios     los picos que no llegaron a incidente         │
    │      ceguera          qué quedó sin poder mirarse                   │
    │      logs             errores extraídos, agrupados por firma        │
    └─────────────────────────────────┬───────────────────────────────────┘
                                      ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  EL BUCLE                          hasta 4 vueltas, después cierra  │
    ├─────────────────────────────────────────────────────────────────────┤
    │                                                                     │
    │    modelo ──▶ "necesito journal(unidad=x)"                          │
    │                        │                                            │
    │                        ▼                                            │
    │              el observador ejecuta y devuelve                       │
    │                        │                                            │
    │                        ▼                                            │
    │    modelo ──▶ "ahora leer(/var/log/x.log, buscar=error)"            │
    │                        │                                            │
    │                        ▼                                            │
    │              ... hasta que responde, o se acaban las vueltas        │
    │                  (en la última se le sacan las herramientas         │
    │                   para forzarlo a concluir con lo que tiene)        │
    └─────────────────────────────────┬───────────────────────────────────┘
                                      ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  LA RESPUESTA                                 JSON estricto         │
    ├─────────────────────────────────────────────────────────────────────┤
    │  es_real             true | false                                   │
    │  confianza           alta | media | baja                            │
    │  causa_probable      una o dos frases                               │
    │  accion_sugerida     qué conviene hacer                             │
    │  explicacion_descarte  sólo si es_real = false                      │
    │  resumen             sólo en modo periódico                         │
    └─────────────────────────────────────────────────────────────────────┘
```

### Las herramientas

Cinco, **todas de lectura**. No hay forma de escribir, borrar ni ejecutar.

```
   listar(ruta)                      qué hay en un directorio
   leer(ruta, buscar, lineas)        un archivo; con `buscar`, sólo lo que importa
   telemetria(que)                   memoria, discos, carga y puertos AHORA
   journal(unidad, solo_errores)     los últimos mensajes de systemd
   consultar(sql, limite)            SQL de lectura contra la base de la plataforma
```

Las cuatro primeras son del núcleo. `consultar` la aporta el adaptador de la
aplicación (`herramientas()`, opcional en el contrato): una sola sentencia
`SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN`, cortada por el servidor a los 10 s,
hasta 100 filas, devuelta como texto `columna=valor | ...` y redactada. Lo que
impide escribir no es el filtro sino el usuario de la base (`install/sql/001`).
Existe porque la respuesta a "¿está congelada la ingesta?" estaba en dos tablas
y el agente sólo podía leer logs (srv2, 2026-09-17).

`buscar` con un número solo lo busca como palabra entera: "401" ya no trae
`quedan 2401s` ni `en 401ms`, que es de donde salió un 401 inventado.

**Puede moverse por donde necesite.** No hay lista de rutas permitidas. Lo que lo
orienta es el bloque `mapa:` del YAML del host, que de cada ruta dice qué es, qué
va a encontrar y **cuándo conviene mirarla**.

Eso último no es decoración: probado a ciegas, con la causa de un problema en un
log que no figuraba en ningún lado, el agente **no la encontró** mientras el mapa
listaba sólo los logs conocidos. Agregando `/var/log` como raíz —con la nota de
que ahí están todos los logs, no sólo los nuestros— la encontró y dio la causa
exacta. **El mapa define el alcance real de la exploración aunque técnicamente no
restrinja nada: lo que no está en el mapa, para el agente no existe.**

Dos límites, y ninguno restringe dónde puede mirar:

- **Los archivos que son puro secreto no se leen** (`.env`, claves privadas,
  `shadow`). No porque el agente no deba mirar ahí, sino porque su contenido
  viajaría al proveedor del modelo en la respuesta y ningún diagnóstico lo
  necesita.
- **Lo que vuelve pasa por el redactor**, igual que el resto de la evidencia:
  se quitan credenciales, teléfonos y correos de clientes finales.

### Lo que el modelo NO puede hacer

Puede marcar un incidente como falso positivo, y para eso tiene tres candados:

```
   1. sólo con confianza ALTA
   2. nunca sobre un control crítico, ni sobre SVC-01, SVC-05, DB-01,
      HST-07, HST-09, HST-12, HST-13
   3. descartar NO es cerrar: el incidente sigue abierto, sigue midiéndose
      y sigue apareciendo en el informe, en su propia sección
```

Un modelo que se equivoca no puede ser la razón por la que nadie se enteró de
que se cayó producción.

### La revalidación antes del informe

El diagnóstico se hace una vez, al abrir. Hasta el 2026-09-17 el informe lo
repetía todos los días: en srv2 dos trabajos ya arreglados salían con la causa
del día en que fallaron, y un "401 de Telefonia" sacado de `quedan 2401s`
salía como hecho. Ahora (`sysop/revalidacion.py`), antes de armar el correo,
el agente mira **otra vez** cada incidente abierto, en modo `revalidar`, y
contesta un veredicto con evidencia:

```
   real                  sigue            -> lista principal
   no_se_pudo_validar    no hay conclusión -> lista principal, pero no sube
                                              el asunto por encima de WARNING
   falsa_alarma          no es problema   -> sección "Falsas alarmas"
   arreglado_pendiente   la causa ya no   -> sección "Arreglados, falta
                         está; falta la      confirmar" HASTA una fecha; si
                         próxima corrida     para entonces sigue abierto,
                                             vuelve a la principal
```

**Las citas las verifica el código** (`esquema.verificar`). Cada cita tiene
que aparecer en lo que el agente miró en esa misma respuesta: textual, o con
todos sus pares `clave: valor` juntos en un mismo resultado (un JSON se cita
con otro orden de claves). Con eso:

- `falsa_alarma` y `arreglado_pendiente` necesitan confianza alta **y** una
  cita de algo mirado ahora con una herramienta (el paquete es la medición
  vieja). Si no, pasan a `no_se_pudo_validar`, y el correo dice por qué.
- un `real` sin ninguna cita verificable sigue en la lista, pero su causa no
  se publica.

**La fecha de "falta confirmar"** la calcula el adaptador cuando el recurso es
un trabajo (`confirmacion()`: la próxima corrida según su cron, más entre 2 y
12 horas de margen); si no, la que propone el agente o un día. Tope 8 días. Se
fija la primera vez y no se corre: recalcularla en cada informe la mandaría a
"la próxima corrida" para siempre.

**Una persona puede decir "ya lo arreglé"** por Telegram
(`marcar_arreglado`, con motivo obligatorio): cuenta como un arreglado con su
nombre, y el agente lo comprueba igual. Se deshace con `desmarcar_arreglado`.

Lo que no cambia: lo crítico y la lista dura quedan en la lista principal diga
lo que diga el agente o la persona; ningún veredicto cierra un incidente; las
alertas urgentes no pasan por acá. Se apaga con `llm.revalidar.habilitada:
false` y se acota con `max_por_informe` (15) y `max_seg` (900).

Para ver qué diría el correo sin mandarlo:
`sysop-agent informe --vista-previa /ruta/informe.html` (revalida, paga las
llamadas y guarda los veredictos, pero no encola ni avanza los cursores).

### Cuándo se llama y cuánto cuesta

Al **abrir** un incidente, una vez por incidente abierto en cada informe, y una
vez por la lectura del período. Un día sano no gasta nada.

```
   sin herramientas       ~1.700 tokens     0,0011 USD     4 s
   con búsqueda          ~28.500 tokens     0,0088 USD     9 s
   revalidación          ~34.000 tokens     0,0110 USD    13 s   (srv3, 17/09)
   informe del período   ~57.500 tokens     0,0178 USD    20 s
```

El salto es inherente al *function calling*: el resultado de cada herramienta
queda en el historial y se reenvía en cada vuelta siguiente. Por eso los topes de
lectura son chicos a propósito (8 KB por archivo).

---

## 6. Los dos canales de salida

Son independientes a propósito, y esa independencia importa:

```
   ┌────────────────────────────────────────────────────────────────────┐
   │  ALERTA URGENTE                              la dispara el daemon  │
   ├────────────────────────────────────────────────────────────────────┤
   │  cuándo    un incidente CRÍTICO se abre, pasada la histéresis      │
   │  cuántas   una por incidente, nunca repetida                       │
   │  contiene  el incidente, qué más estaba roto al mismo tiempo,      │
   │            y el diagnóstico del modelo                             │
   │  existe    mientras el servicio corra. No depende de nada más      │
   │  apagar    `alertas: {urgentes: false}` en el YAML, para una       │
   │            ventana de mantenimiento. No para el uso normal         │
   └────────────────────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────────────────────┐
   │  INFORME PERIÓDICO                                lo dispara CRON  │
   ├────────────────────────────────────────────────────────────────────┤
   │  cuándo    lo que diga /etc/cron.d/sysop-agent. Nada más            │
   │  sale      SIEMPRE, aunque esté todo bien                          │
   │  cubre     desde el informe anterior hasta ahora                   │
   │  contiene  la lectura del modelo, incidentes, transitorios,        │
   │            ceguera, errores de los logs y el estado actual         │
   │  si no     no pasa nada: el resto sigue funcionando igual          │
   │  se usa                                                            │
   └────────────────────────────────────────────────────────────────────┘
```

**Por qué el informe lo dispara cron y no el daemon.** Si lo mandara el propio
daemon y el daemon se muriera, no llegaría nada — y no llegar nada es
indistinguible de que esté todo bien. Disparado por cron, el trabajo corre igual,
mira el último estado conocido y manda el correo **diciendo que el observador
está caído**. Eso hace de latido sin ningún proceso vigilando desde afuera.

**La cadencia vive en una sola línea** y en ningún otro lado:

```cron
0 11 * * *  sysop-agent  /opt/sysop-agent/bin/informe.sh informe \
                        >> /var/log/sysop-agent/informe.log 2>&1
```

Pasar de diario a semanal es editar esa expresión. La ventana del informe se
calcula desde el último enviado, así que el período se ajusta solo.

**El envoltorio `bin/informe.sh` no es un detalle.** Cron no carga el
`EnvironmentFile` de la unidad systemd: sin él, el informe salía leyendo la
última copia buena de la configuración en vez de la base, sin un solo error que
lo dijera.

### Qué trae el informe

```
   ┌──────────────────────────────────────────────────────────┐
   │  la lectura del modelo sobre el período entero           │
   ├──────────────────────────────────────────────────────────┤
   │  QUÉ PASÓ desde el informe anterior                      │
   │    · incidentes: nuevos, resueltos, sin resolver         │
   │    · TRANSITORIOS: fallas que no llegaron a incidente    │
   │    · CEGUERA: qué quedó sin poder mirarse, y por qué     │
   │    · errores de los logs, agrupados por firma            │
   ├──────────────────────────────────────────────────────────┤
   │  ESTADO AHORA                                            │
   │    · controles en ok / problema / unknown / sin datos    │
   │    · incidentes activos, revalidados para este informe   │
   │    · arreglados, falta confirmar (con fecha)             │
   │    · falsas alarmas, con el motivo y la cita             │
   └──────────────────────────────────────────────────────────┘
```

Los dos del medio son los que no se ven de ninguna otra forma. Un servicio que
se cae **una sola muestra** a las tres de la mañana y vuelve solo no abre
incidente —y está bien que no lo abra— pero que haya pasado diecisiete veces en
la semana es un dato.

### El canal de vuelta: Telegram

Los dos canales de arriba son de ida. El de Telegram es el de vuelta: llega el
correo, y desde el teléfono se le pregunta al mismo agente qué está viendo.

```
   ┌────────────────────────────────────────────────────────────────────┐
   │  CONVERSACION                                    la inicia quien   │
   │                                                  recibe el correo  │
   ├────────────────────────────────────────────────────────────────────┤
   │  proceso   sysop-agent-bot.service, APARTE del observador           │
   │  quien     solo los chat_id de `autorizados` en telegram.yaml      │
   │  puede     leer los informes que mandó, los incidentes abiertos,   │
   │            los logs, el journal y la telemetría de ahora           │
   │  no puede  ejecutar nada. Las mismas herramientas de lectura del   │
   │            diagnóstico, con el mismo filtro de secretos            │
   │  gasto     presupuesto propio, separado del de los diagnósticos    │
   │  apagar    `habilitado: false` en config/telegram.yaml             │
   └────────────────────────────────────────────────────────────────────┘
```

**Por qué es un servicio aparte y no un hilo del observador.** El momento en que
uno le escribe al bot es casi siempre el momento en que algo se rompió. Si el
bot viviera adentro del observador, la excepción que se lleve al observador se
lleva también el canal por el que ibas a preguntar qué pasó. Son dos funciones
con modos de falla distintos, así que son dos unidades con su propio `Restart`.
El bot **no** declara `Requires=sysop-agent.service`: tiene que contestar aunque
el observador esté caído, porque lo que lee es el almacén, que sigue ahí.

**Por qué long polling y no un webhook.** Un webhook necesita el servidor
publicado en internet con TLS, y el observador no abre ni un puerto. Con
`getUpdates` sigue funcionando aunque nginx esté caído, que es justamente uno de
los momentos en los que uno quiere preguntar.

**La lista blanca no es opcional.** Cualquiera que sepa el nombre del bot puede
escribirle: Telegram no permite esconderlo. Del otro lado hay un agente con
lectura sobre un servidor de producción, así que `autorizados` es el único
control que lo separa de internet. Con la lista vacía el bot arranca y no le
contesta a nadie, a propósito. A un chat no autorizado **no se le responde
nada** —contestar "no estás autorizado" ya confirma que hay algo del otro
lado—; su `chat_id` queda en el log, que es como se averigua el propio.

**El contexto sale de donde ya estaba.** El pedido era poder leer el correo que
acaba de llegar y seguir desde ahí, y no hizo falta guardar nada nuevo:
`outbox.body_html` ya tenía el HTML de cada informe enviado, con 90 días de
retención. La herramienta `informes` lo convierte a texto y se lo da al modelo.

El prompt del chat es **propio y distinto** del de diagnóstico
(`config/prompts/telegram.md`): el diagnóstico emite JSON que consume el código,
y esto lo lee una persona en un teléfono. La credencial y el modelo se reusan
del agente del observador, con su mismo respaldo, así que el bot sigue
contestando con la base caída.

Para verificarlo sin levantar el servicio:

```
sysop-agent telegram              token, autorizados y prompt
sysop-agent telegram --mandar     ademas manda un mensaje de prueba
```

---

## 7. Qué usa

### Dependencias de Python

Tres, y ninguna es un framework:

```
   mysql-connector-python    leer la base de la plataforma
   httpx                     endpoints internos del back, y la API del modelo
   pyyaml                    la configuración
```

No usa FastAPI, uvicorn ni jinja2: el panel web se dio de baja el 2026-08-28 y
la salida es correo y terminal. Menos superficie que actualizar y un puerto menos
abierto en cada servidor observado.

### Del sistema

```
   /proc/meminfo, /proc/1/mountinfo    memoria y mounts
   /proc/locks                         bloqueos de trabajos, por inodo
   /proc/<pid>/cmdline                 identidad de los workers de voz
   statvfs                             uso de disco e inodos
   systemctl show                      estado de las unidades
   journalctl                          eventos del kernel y de servicios
   netlink                             puertos escuchando
   findmnt, timedatectl                mounts y sincronización de reloj
```

### De la plataforma

```
   MySQL (solo lectura)                usuario sysop_ro
   GET /api/health                     que el back atienda de verdad
   GET /api/internal/observability/*   workers esperados, contratos de
                                       trabajos, backlog del pipeline
   GET :11000/api/health               el sidecar, para saber qué workers
                                       están conectados
   tabla agents + llm_credentials      el agente que diagnostica
   email_report_recipients             a quién avisar
   client_config_vars                  la cuenta de correo
```

**Todo lo que lee de la plataforma tiene caída.** Si el back no responde, se usa
SQL directo con una consulta congelada y **se marca la evidencia como `PARTIAL`**.
Si la base tampoco responde, se usa la última copia buena de la configuración y
el correo lo declara. El observador tiene que seguir mirando justamente cuando la
plataforma está caída.

### Permisos que necesita

```
   MySQL       SELECT sobre la base + SELECT sobre performance_schema
               + PROCESS                        ← sin esto, cinco controles
                                                  son imposibles
   sistema     grupo systemd-journal             ← sin esto, journalctl
                                                  devuelve vacío en silencio
   lectura     /var/log, /proc, /etc, /tmp
   escritura   sólo /var/lib/sysop-agent y /var/log/sysop-agent
```

Corre como el usuario `sysop-agent`, sin shell, sin login y fuera del grupo de
Docker. La unidad tiene `NoNewPrivileges`, `ProtectSystem=strict` y
`ProtectHome`, con `PrivateTmp=no` porque necesita leer los `.pid` de `/tmp`.

**No toca `/opt/plataforma`.** Se despliega por su propio canal, nunca por el
`git pull` de la plataforma: tiene que seguir en pie justamente cuando ese
despliegue sale mal.

---

## 8. Cómo se configura

Cuatro lugares, y cada uno tiene una razón para existir por separado.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  config/so.yaml                                          va a git   │
   │  Lo del sistema operativo. No nombra ninguna aplicación.            │
   │  Umbrales de disco, memoria, inodos; límites del propio observador. │
   └─────────────────────────────────┬───────────────────────────────────┘
                                     │  se aplica encima
   ┌─────────────────────────────────▼───────────────────────────────────┐
   │  config/app.yaml                                         va a git   │
   │  Lo de la aplicación. `app: plataforma` elige el adaptador.     │
   │  Umbrales de trabajos, colas, workers, costo. Y las FUENTES.        │
   └─────────────────────────────────┬───────────────────────────────────┘
                                     │  se aplica encima y gana
   ┌─────────────────────────────────▼───────────────────────────────────┐
   │  config/hosts/<host>.yaml                                va a git   │
   │  Lo propio de ESE servidor: qué unidades se esperan activas, cuáles │
   │  detenidas, cuáles se ignoran y por qué, qué logs administra,       │
   │  y el MAPA que orienta al agente.                                   │
   └─────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────┐
   │  /etc/sysop-agent/sysop-agent.env                       NO va a git   │
   │  Sólo secretos y rutas: credencial de MySQL, clave interna del      │
   │  back, host-id. Cargado por la unidad systemd y por bin/informe.sh  │
   └─────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────┐
   │  /etc/cron.d/sysop-agent                          se edita a mano    │
   │  La cadencia del informe. Vive acá y en ningún otro lado.           │
   └─────────────────────────────────────────────────────────────────────┘
```

### Las fuentes: de dónde salen el agente, los destinatarios y el correo

Es el bloque que hace que el núcleo no dependa de una plataforma en particular.
Cada una se declara con un tipo, y el adaptador la resuelve:

```yaml
fuentes:
  timezone: America/Argentina/Buenos_Aires

  agente:
    tipo: mysql              # lo saca de la tabla `agents`
    tenant: acme
    agent_id: SYSOP
    respaldo: base.yaml      # y esto es lo que se usa si la base no responde

  destinatarios:
    tipo: mysql              # de email_report_recipients, scope 'sysop'
    tenant: acme
    scope: sysop
    respaldo: base.yaml

  correo:
    tipo: mysql              # de client_config_vars: MAIL_HOST, MAIL_USER...
    tenant: acme
    respaldo: base.yaml
```

En una plataforma que no tenga tablas de agentes, el mismo bloque dice:

```yaml
fuentes:
  agente:
    tipo: archivo
    prompt: prompts/sysop.md               # relativa a config/, o absoluta
    modelo: gemini-3-flash-preview
    api_key_env: SYSOP_LLM_API_KEY         # el NOMBRE de la variable, no la clave

  destinatarios:
    tipo: archivo
    lista: [alguien@ejemplo.com]
```

El núcleo pide `obtener_agente()` y le llega. Nunca sabe de dónde salió.

**La cadena son DOS eslabones y los dos están declarados en el YAML.** Mientras
MySQL conteste manda MySQL, que es lo que se edita desde la pantalla de la
plataforma. Cuando no contesta manda `config/base.yaml`, que se versiona, se lee
de un `cat` y se edita a mano. El correo declara cuál de los dos se usó.

Si la configuración del que avisa vive en la base que vigila, la caída de esa
base lo dejaría mudo justo cuando hace falta.

**Por qué un archivo y no una caché.** Hasta el 2026-09-02 el respaldo era una
copia en SQLite que se llenaba sola con la última lectura buena. Se sacó por tres
razones medidas:

- No era una garantía sino un recuerdo. Con el almacén vacío y la base caída,
  `puede_notificar` daba False: una instalación nueva quedaba muda y nadie se
  enteraba hasta el día que hacía falta.
- Era estado invisible que juntaba basura. Al sacarla, la tabla tenía dos claves
  de un diseño anterior (`notify:smtp`, `notify:destinatarios`) que nadie leía ni
  escribía desde hacía cinco días, con la password SMTP en claro adentro.
- Duplicaba secretos en un tercer lugar, además de la base y el entorno.

**El precio, y cómo se paga.** El archivo se mantiene a mano, así que puede
quedar viejo. Eso no se corrige solo a propósito: se avisa. En cada informe el
observador compara lo que dice la base contra lo que dice el archivo, y publica
las diferencias mientras todavía no hacen falta. Para verificarlo sin esperar al
correo, y sin tocar la base:

```
sysop-agent config              qué configuración está usando ahora
sysop-agent config --respaldo   simula que la base no responde y usa sólo el
                               archivo: dice si alcanza por sí solo
```

Las dos credenciales del respaldo van por variable de entorno, nunca en el YAML,
que va a git: `SYSOP_LLM_API_KEY` y `SYSOP_SMTP_PASSWORD` en
`/etc/sysop-agent/sysop-agent.env`.

### El mapa del servidor

Va en el YAML del host. No restringe: orienta. De cada ruta contesta tres
preguntas, y las tres importan:

```yaml
mapa:
  que_es_este_servidor: >
    PRODUCCION de Plataforma. Atiende llamadas telefónicas REALES.
    ANTES DE SUGERIR CUALQUIER ACCION: una llamada en curso se corta si se
    reinicia un worker o el supervisor de líneas.

  rutas:
    - ruta: /var/log
      que_es: "TODOS los logs del servidor, no sólo los nuestros."
      que_vas_a_encontrar: >
        Los de la plataforma, los del sistema, los de nginx y MySQL, y los de
        cualquier otra aplicación instalada, esté declarada acá o no.
      cuando_mirarlo: >
        LISTALO PRIMERO ante un componente que no figura en esta lista.
```

**`que_es`** para que entienda qué está leyendo, **`que_vas_a_encontrar`** para
que sepa si le sirve, y **`cuando_mirarlo`** para que no lo abra siempre.

Al escribir el mapa de un servidor nuevo, la regla es **declarar las raíces, no
sólo lo conocido**.

### Los umbrales

Todos son configurables y ninguno está en el código. Los que más se tocan:

```yaml
filesystems:
  umbrales_uso_pct:            {warning: 85, major: 92, critical: 97}
  umbrales_dias_para_llenar:   {warning: 30, major: 14, critical: 3}

db:
  umbrales_conexiones_pct:     {warning: 70, major: 85, critical: 95}
  umbrales_consulta_seg:       {warning: 30, major: 120, critical: 300}

jobs:
  gracia_lanzamiento_seg: 120        # piso; para un job espaciado se usa
                                     # la mitad de su período
  max_runtime_seg: 3600

colas:
  inbox_edad_max_seg: 300
  processing_lease_seg: 600
  agenda_vencida_seg: 3600

llm:
  presupuesto_diario_usd: 2.0        # agotado, avisa igual sin diagnóstico
  timeout_seg: 30
  sin_filtro: []                     # SUMA a la lista dura del código

alertas:
  urgentes: true
```

---

## 9. Qué tiene adentro

10.810 líneas de Python, 206 pruebas en 11 baterías.

```
sysop-agent/
│
├── sysop/
│   ├── core/                    EL NÚCLEO — no sabe de SO ni de aplicaciones
│   │   ├── models.py            Recurso, Control, Resultado, Incidente
│   │   ├── engine.py            corre un control, persiste, mueve el incidente
│   │   ├── hysteresis.py        cuándo un resultado se vuelve problema
│   │   ├── incidents.py         abrir, actualizar, recuperar, cerrar
│   │   ├── inventory.py         qué recursos hay y cuáles son nuevos
│   │   ├── trend.py             proyección de agotamiento (Theil-Sen)
│   │   ├── scheduler.py         qué controles vencieron, con jitter
│   │   ├── config.py            las tres capas de YAML
│   │   └── fuentes.py           agente, destinatarios y correo: mysql|archivo
│   │
│   ├── adapters/
│   │   ├── so_linux/            EL SISTEMA OPERATIVO — fijo
│   │   │   ├── checks_host.py       HST: memoria, disco, inodos, OOM, reloj
│   │   │   ├── checks_svc.py        SVC: unidades, sockets, endpoints, TLS
│   │   │   └── collectors/          host, systemd, journal, net
│   │   │
│   │   └── apps/                LAS APLICACIONES — enchufables
│   │       ├── base.py              el contrato que cumple un adaptador
│   │       └── plataforma/
│   │           ├── datos.py         los 3 endpoints, con caída a SQL
│   │           ├── checks_db.py     DB: conexión, conexiones, consultas, locks
│   │           ├── checks_jobs.py   JOB: los 8 controles de trabajos
│   │           ├── checks_workers.py WRK: falta, sobra, duplicado, desconectado
│   │           ├── checks_queues.py QUE: inbox, outbox, pipeline, agenda
│   │           ├── checks_ops.py    OPS: logs, costo, salud propia
│   │           ├── contratos.py     qué tiene que haber PRODUCIDO cada trabajo
│   │           ├── cron.py          parser de expresiones cron
│   │           ├── locks.py         /proc/locks por inodo
│   │           └── mysql.py         conexión con timeout y presupuesto
│   │
│   ├── llm/                     EL DIAGNÓSTICO
│   │   ├── gateway.py               la llamada, con presupuesto y corte
│   │   ├── contexto.py              el paquete de evidencia, redactado
│   │   ├── esquema.py               la respuesta, en JSON estricto
│   │   └── herramientas.py          listar, leer, telemetria, journal
│   │
│   ├── notify/                  EL CORREO
│   │   ├── outbox.py                bandeja con reintentos e idempotencia
│   │   ├── render.py                el HTML del informe y de la alerta
│   │   └── smtp.py                  un intento de envío
│   │
│   ├── store/                   EL ALMACÉN
│   │   ├── db.py                    SQLite en modo WAL
│   │   └── schema.sql               19 tablas
│   │
│   ├── daemon.py                el servicio: controles y alertas urgentes
│   ├── informe.py               el informe periódico, disparado por cron
│   ├── periodo.py               qué pasó desde el informe anterior
│   ├── diagnostico.py           cuándo se pregunta y qué se hace con eso
│   ├── alertas.py               las urgencias
│   ├── logs.py                  extraer errores de los logs, agrupados
│   ├── contexto.py              lo que comparten el daemon y el CLI
│   ├── log_setup.py             sus propios logs, según la convención
│   └── cli.py                   estado, probar, informe, diagnosticar, logs
│
├── config/
│   ├── so.yaml                  lo del sistema operativo
│   ├── app.yaml                 lo de la aplicación + las fuentes
│   └── hosts/{srv2,srv3}.yaml     lo de cada servidor + el mapa
│
├── install/
│   ├── install.sh               idempotente, 3 etapas, modo --check
│   ├── sysop-agent.service       la unidad
│   ├── sysop-agent.cron          el informe periódico
│   ├── RUNBOOK.md               por qué hace cada cosa el instalador
│   └── sql/                     usuario de MySQL, permisos, y el agente
│
├── bin/informe.sh               carga el env y llama al comando
├── tests/                       11 baterías, 206 casos
├── CONVENCION-DE-LOGS.md        cómo se escriben los logs de TODAS las apps
├── README.md
└── ARQUITECTURA.md              este archivo
```

### El almacén local

SQLite con WAL en `/var/lib/sysop-agent/sysop.db`. Es lo que garantiza que el
observador siga registrando cuando la base de negocio no responde.

```
   resource            qué existe, y desde cuándo
   expectation         qué debería existir, y quién lo dijo
   check_result        cada medición, con lo esperado y lo medido
   observation         evidencia cruda, append-only
   incident            un problema, con su fingerprint y su ciclo de vida
   incident_event      la línea temporal de cada incidente
   diagnostico         qué dijo el modelo de cada incidente
   llm_call            cada llamada al modelo, con tokens y costo
   outbox              los correos, con idempotencia por período
   cursor              por dónde iba la lectura de cada log y del journal
   baseline            línea base por día y franja horaria
   job_run             corridas de trabajos
   audit               qué se cambió y quién
   maintenance_window  ventanas donde no se avisa
   suppression         silencios con motivo y vencimiento
```

**Retención:** evidencia cruda 7 días, resultados 400 días, correos enviados 90
días. El propio observador tiene un control (`OPS-10`) que vigila que su almacén
no crezca de más: no puede llenar el disco que vigila.

---

## 10. Los 39 controles

De los 100 de la especificación original, la v1 cubre 40 y hay 39 implementados.
Falta `DB-13`, bloqueado hasta saber dónde están los respaldos de producción.

### Sistema operativo — 14

```
   HST-01  el observador dejó de reportar        HST-12  mount ausente o read-only
   HST-05  memoria disponible baja                HST-13  errores de disco
   HST-07  evento OOM nuevo                       HST-17  reloj desincronizado
   HST-09  filesystem lleno                       SVC-01  unidad esperada caída
   HST-10  proyección de agotamiento              SVC-03  bucle de reinicios
   HST-11  inodos                                 SVC-05  socket que no escucha
                                                  SVC-07  endpoint fuera de SLO
                                                  SVC-09  certificado por vencer
```

### Aplicación — 25

```
   DB-01   la base no responde                    QUE-01  bandeja de entrada estancada
   DB-02   conexiones cerca del máximo            QUE-02  mensaje tomado y abandonado
   DB-04   consultas o transacciones largas       QUE-04  salida sin enviar
   DB-05   esperas de bloqueo                     QUE-09  pipeline acumulando
                                                  QUE-10  agenda vencida
   JOB-01  apareció o desapareció un trabajo
   JOB-03  trabajo activo sin ejecutable          WRK-01  no se puede derivar el conjunto
   JOB-05  no se lanzó cuando debía               WRK-02  worker esperado que falta
   JOB-06  se lanzó y no volvió                   WRK-03  worker que sobra
   JOB-07  volvió con error                       WRK-04  worker duplicado
   JOB-08  volvió bien y NO produjo nada          WRK-05  worker desconectado del sidecar
   JOB-10  bloqueo abandonado
   JOB-14  el planificador está caído             OPS-01  logs sin rotar
                                                  OPS-08  costo de modelos
                                                  OPS-10  salud del propio observador
```

**`JOB-08` es el más valioso y el más caro.** Verifica el *resultado*, no la
ejecución: que `syncLastCalls` le haya sacado una respuesta a Telefonia, que
la cohorte elegible de `procesarLlamadas` haya bajado. Un trabajo que se lanza
puntual, sale con código cero y no produce nada pasa limpio por los otros cinco
controles de su familia.

Para el sync, lo que cuenta es la **respuesta**, no que haya llamadas nuevas.
Hasta el 2026-09-17 se miraba la última llamada guardada contra un tope de 180
minutos, y un cliente sin llamadas de noche salía como "ingesta congelada" con
el sync respondiendo bien cada diez minutos. Ahora se lee el log del sync: cada
corrida deja `get_records_telefonia: N records` (RingEX) o `N llamadas
obtenidas en total` (RingCX), también con N = 0. Tres corridas seguidas sin
ninguna (`jobs.contratos.sync_corridas_sin_respuesta`) es
`TELEFONIA_SIN_RESPUESTA` si el sync escribió algo, o `SYNC_SIN_ACTIVIDAD` si
no escribió nada. Sin el archivo, `UNKNOWN`. Esos dos mensajes del sync son el
contrato: si se cambian en `acmeBot/services/`, se cambian en `contratos.py`.

**`JOB-10` se hace distinto de lo que decía la especificación.** Consultar
`/proc/locks` por inodo, nunca intentar tomar el bloqueo: si en ese instante no
hay nadie corriendo, el observador se lo queda y el trabajo que arranque después
muere. El observador provocaría la falla que dice observar.

---

## 11. Cómo se opera

No hay panel web ni puerto abierto. Se mira desde el servidor:

```bash
sysop-agent estado         # qué ve el observador ahora
sysop-agent probar         # corre un ciclo completo SIN guardar ni mandar nada
sysop-agent logs           # los errores de los logs declarados, agrupados
sysop-agent diagnosticar   # le pide al modelo que explique lo que hay abierto
sysop-agent informe        # arma y manda el informe (es lo que llama el cron)
```

`probar` es el que hay que correr después de tocar un umbral o un YAML: muestra
qué contestaría cada control ahora mismo, contra el servidor de verdad, sin
efectos.

### Desplegar un cambio

```bash
cd /opt/acme/plataforma/sysop-agent
tar --exclude=venv --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=sysop_agent.egg-info -cf - . \
  | ssh srv3 'rm -rf /tmp/sysop_src && mkdir -p /tmp/sysop_src \
             && tar -xf - -C /tmp/sysop_src'
ssh srv3 'SYSOP_SRC=/tmp/sysop_src bash /tmp/sysop_install.sh --host-id srv3 --stage app
         systemctl restart sysop-agent'
```

El instalador es idempotente, tiene un modo `--check` que no toca nada, y corta
antes de instalar la unidad si el YAML del host no existe o si el paquete no
importa.

### Verificar la sobrecarga

El criterio de aceptación es **1% de un core y 150 MiB**. Medido en srv3 con 79
controles: **0,79% y 36 MiB**. El margen de CPU no es cómodo, así que conviene
medirlo en cada servidor nuevo, con una ventana de cinco minutos:

```bash
a=$(systemctl show sysop-agent -p CPUUsageNSec --value); t0=$(date +%s%N)
sleep 300
b=$(systemctl show sysop-agent -p CPUUsageNSec --value); t1=$(date +%s%N)
python3 -c "print(f'{($b-$a)/($t1-$t0)*100:.2f}% de un core')"
```

Una ventana de treinta segundos engaña: los controles de cinco minutos vencen
juntos y el número sale del doble o de la mitad según dónde caiga.

---

## 12. Las decisiones que explican el diseño

Puestas acá porque son las que uno se pregunta al leer el código.

**Por qué no hay nodo central.** Cada instalación es autónoma. Diez servidores
son diez instalaciones iguales que no se conocen entre sí. El hueco que eso deja
—si el servidor se apaga entero, no llega nada— lo cubre el informe por cron, que
sale igual y avisa que el observador no está.

**Por qué el plano determinístico decide y el modelo explica.** Ningún estado lo
fija un modelo de lenguaje. Los umbrales, la histéresis y los incidentes se
resuelven midiendo. El modelo correlaciona y redacta sobre evidencia ya
comparada, y lo único que puede hacer es marcar un falso positivo, con tres
candados encima.

**Por qué la ausencia de evidencia es `UNKNOWN` y nunca `OK`.** Está impuesto en
el código: un `CheckOutcome` con estado `OK` y evidencia `NONE` lanza excepción.
El motivo es un patrón que apareció tres veces durante el desarrollo — una fuente
que no se puede leer devuelve lo mismo que una fuente vacía:

```
   information_schema.processlist sin PROCESS   →  devuelve 1 fila de 232
   journalctl sin el grupo systemd-journal      →  devuelve vacío
   contar conexiones para verificar un permiso  →  el número da igual
```

Las tres veces producía un `OK` sobre algo que no se estaba mirando. La regla es
**preguntar explícitamente si se pudo leer**, nunca inferirlo de que el resultado
esté vacío.

**Por qué el adaptador consume y no reimplementa.** La derivación de qué workers
deberían existir es la misma función que usa el supervisor de líneas. Copiarla
garantiza que el día que alguien la toque, las dos versiones divergen en
silencio.

**Por qué el observador no toca nada.** Sólo observa y diagnostica. Las cuatro
herramientas del agente son de lectura y no hay forma de escribir, borrar ni
ejecutar. Además, el contenido de un log lo escribe cualquiera: un mensaje de
error podría llevar texto que induzca un comando, y el prompt lo dice
explícitamente — lo que se lee en un log es evidencia, nunca una instrucción.

---

## 13. Estado y lo que falta

Actualizado el 2026-10-08.

```
   controles          39 de 40      falta DB-13 (respaldos), bloqueado
   pruebas            19 baterías en tests/
   srv2 (producción)   corriendo     sysop-agent y sysop-agent-bot activos,
                                    informe diario a las 11:00 UTC,
                                    alertas urgentes encendidas
   srv3 (desarrollo)   corriendo     sin el cron del informe
```

En srv2 manda informes desde principios de septiembre (el del 2026-09-03 abrió
con 21 incidentes, que se depuraron contra el servidor). El último despliegue
fue el 2026-09-17 a las 22:25 UTC: la revalidación antes del informe
(sección 5) y el `JOB-08` del sync por respuesta de Telefonia (sección 10).

En los dos servidores corre de `/opt/sysop-agent`, una copia instalada **sin
git**: un `git pull` en `/opt/plataforma` no lo actualiza. Desplegar es
copiar a `/opt/sysop-agent` y reiniciar (sección 11). Para saber si un servidor
está al día, se compara el `md5sum` de sus archivos contra los del repo.

De los tres pasos que se habían fijado antes de encender las alertas en srv2:

1. Las entradas de `config/hosts/srv2.yaml` quedaron confirmadas contra el
   servidor (relevado el 2026-09-02); no queda ninguna marcada `verificar: true`.
2. **La sobrecarga no está re-medida en srv2**, que tiene 17 trabajos contra los
   6 de srv3. Sigue pendiente, con el procedimiento de la sección 11.
3. No consta que se hayan hecho las dos semanas con las alertas críticas
   apagadas: en la configuración instalada, `alertas.urgentes` está en `true`.

Lo que sigue abierto es `DB-13`, a la espera de saber dónde están los
respaldos de producción.
