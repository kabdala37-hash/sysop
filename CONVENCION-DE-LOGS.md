# Cómo se escriben los logs

**Este archivo es la referencia única.** Cualquier aplicación nuestra —la que
existe y la que venga— escribe sus logs así. No es una preferencia de estilo: es
lo que hace que el análisis automático del sistema sea exacto en vez de una
adivinanza.

> **Esto es una especificación, no una librería.** No hay un módulo compartido
> que haya que importar, ni una dependencia que agregar. Cada aplicación
> implementa su propio logger, en su lenguaje y a su manera, respetando el
> formato de acá. La sección **Implementación de referencia** trae el código
> listo para copiar en Python y en Node.
>
> Cuando se arranque un programa nuevo, la forma de usar esto es simple:
> pasarle este archivo. Es autosuficiente — el formato, los niveles, las claves
> de contexto, qué no se loguea y la rotación están todos acá.

La regla que resume todo:

> Una línea es un evento. El evento dice **cuándo**, **qué tan grave**, **quién
> lo escribió**, **sobre qué** y **qué pasó** — y los datos variables van
> aparte del mensaje.

---

## El formato

```
<timestamp> <NIVEL> <app>/<componente> <contexto> | <mensaje> | <datos>
```

Un ejemplo real:

```
2026-08-28T15:33:18.482-03:00 ERROR plataforma/channel.worker tenant=clienteb trace=a3f21c9 | no se pudo entregar el mensaje | mensaje_id=442 intento=3 proveedor=twilio
```

| Campo | Obligatorio | Qué es |
|---|---|---|
| `timestamp` | sí | ISO-8601 **con zona horaria**. `2026-08-28T15:33:18.482-03:00` o con `Z` |
| `NIVEL` | sí | Uno de: `DEBUG INFO WARNING ERROR CRITICAL`. En mayúsculas |
| `app/componente` | sí | Qué escribió. `plataforma/channel.worker`, `sysop-agent/engine` |
| `contexto` | no | Pares `clave=valor` que identifican **sobre qué** actúa |
| `\| mensaje` | sí | Texto plano, **sin datos variables** |
| `\| datos` | no | Pares `clave=valor` con lo variable |

### Por qué el mensaje no lleva datos

Esta es la regla que más se viola y la que más cuesta después.

```
mal:   no se pudo entregar el mensaje 442 al lead +5491100000000 (intento 3)
bien:  no se pudo entregar el mensaje | mensaje_id=442 intento=3
```

Con la primera forma, cuarenta y siete fallas iguales son cuarenta y siete
mensajes distintos: no se pueden contar, no se pueden agrupar y no se puede
decir "esto viene pasando desde el martes". Con la segunda, el mensaje **es** la
firma del problema y el conteo sale solo.

De paso: el teléfono del ejemplo no tenía por qué estar ahí. Ver la sección de
qué no se loguea.

### Claves de contexto que usamos siempre igual

Que se llamen igual en todas las aplicaciones es la mitad del valor: permite
cruzar el log de un componente con el de otro.

| Clave | Cuándo | Ejemplo |
|---|---|---|
| `tenant` | todo lo que sea por cliente | `tenant=clienteb` |
| `trace` | id que atraviesa componentes | `trace=a3f21c9` |
| `run` | una corrida de un trabajo programado | `run=8814` |
| `call` | una llamada telefónica | `call=RC-882hd` |
| `job` | el nombre del trabajo | `job=syncLastCalls` |
| `user` | quién lo pidió, si fue una persona | `user=operador` |

**`trace` es el que más se subestima.** Un mensaje entra por el canal, lo procesa
el daemon, dispara una llamada al modelo y termina en una fila de la base: son
cuatro componentes y cuatro archivos distintos. Con el mismo `trace` en los
cuatro, reconstruir qué pasó es un `grep`. Sin él, es una tarde.

---

## Los niveles, y qué significa cada uno

Un nivel mal usado es peor que no tener niveles: entrena a ignorar el que
importa.

| Nivel | Cuándo | Qué implica |
|---|---|---|
| `DEBUG` | detalle para desarrollar | **No va a producción encendido** |
| `INFO` | un hecho normal que interesa después | "arrancó", "procesó 340", "terminó" |
| `WARNING` | algo anómalo que **se resolvió solo** | un reintento que funcionó, un valor raro pero tolerable |
| `ERROR` | una operación **falló** | ese mensaje no se entregó, ese job no terminó |
| `CRITICAL` | el componente **no puede seguir** | no hay base, no hay credencial, se cae |

Dos errores comunes:

- **`ERROR` para algo que se reintentó y salió bien.** Eso es `WARNING`. Si cada
  reintento exitoso es un error, el correo de la mañana tiene cien errores y
  ninguno importa.
- **`INFO` para algo que falló.** Si una operación no se completó, es `ERROR`,
  aunque el proceso siga vivo.

---

## Excepciones y tracebacks

El traceback va **debajo del evento, indentado**. Nunca suelto.

```
2026-08-28T15:33:18.482-03:00 ERROR plataforma/channel.daemon tenant=acme | fallo el turno de la conversacion | conv=8812
    Traceback (most recent call last):
      File "/opt/plataforma/build/services/channel_worker.py", line 214, in turno
        resp = self._llamar_modelo(mensajes)
    mysql.connector.errors.DatabaseError: 1129 (HY000): Host is blocked
```

**Por qué importa.** Hoy, en la plataforma, los tracebacks se escriben crudos y
sin prefijo. Lo que se ve al buscar errores es esto:

```
mysql.connector.errors.DatabaseError: 1129 (HY000): Host '...' is blocked
```

Sin fecha, sin nivel y sin saber qué componente lo escribió. Indentando el
traceback debajo de su evento, la línea que dice **cuándo y quién** viaja pegada
a la que dice **qué**.

En Python sale gratis: `logger.exception("mensaje")` o
`logger.error("mensaje", exc_info=True)`.

---

## Qué NO se loguea, nunca

- **Credenciales**: contraseñas, API keys, tokens, cookies de sesión, cadenas de
  conexión completas.
- **Datos del cliente final**: teléfonos, correos, direcciones, documentos. Si
  hace falta identificar una fila, va el **id**, no el dato.
- **Cuerpos completos** de request, response o mensaje. Va el tamaño y el
  identificador; el cuerpo entero llena el disco y filtra lo de arriba.

Un log lo lee más gente de la que uno cree, y este sistema además se lo manda a
un modelo. Nada que no publicarías.

---

## Dónde y con qué rotación

```
/var/log/<app>/<componente>/<algo>.log
```

- Un archivo por componente. Si es por cliente, `client_<tenant>.log`.
- **Rotación obligatoria**, y no opcional: hoy `/var/log/plataforma` tiene
  archivos de 8 MB sin rotar porque nadie la configuró.
  - Python: `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)`
  - Node y el resto: una regla en `/etc/logrotate.d/<app>`
- A `stdout` también, si corre bajo systemd: el journal es la segunda copia y
  sobrevive a que alguien borre el archivo.

---

## Implementación de referencia

### Python

```python
import logging
from logging.handlers import RotatingFileHandler

FORMATO = "%(asctime)s %(levelname)s " + APP + "/%(name)s | %(message)s"
FECHA   = "%Y-%m-%dT%H:%M:%S%z"

def configurar(app: str, componente: str, ruta: str, nivel: str = "INFO"):
    fmt = logging.Formatter(
        f"%(asctime)s %(levelname)s {app}/%(name)s | %(message)s", FECHA)
    fh = RotatingFileHandler(ruta, maxBytes=10_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(nivel)
    root.addHandler(fh)
    root.addHandler(logging.StreamHandler())   # y al journal

# uso
log = logging.getLogger("channel.worker")
log.info("mensaje entregado | mensaje_id=%s intento=%s", mid, intento)
log.exception("fallo el turno | conv=%s", conv_id)   # el traceback va solo
```

Para el contexto fijo (`tenant`, `trace`) conviene un `LoggerAdapter` en vez de
repetirlo en cada llamada:

```python
log = logging.LoggerAdapter(logging.getLogger("channel.worker"),
                            {"ctx": f"tenant={tenant} trace={trace}"})
```

### Node

```javascript
const NIVELES = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

function crearLogger({ app, componente, archivo, contexto = {} }) {
  const ctx = Object.entries(contexto).map(([k, v]) => `${k}=${v}`).join(' ');
  const escribir = (nivel, mensaje, datos) => {
    const ts = new Date().toISOString();
    const kv = datos
      ? Object.entries(datos).map(([k, v]) => `${k}=${v}`).join(' ') : '';
    const linea = `${ts} ${nivel} ${app}/${componente}` +
                  `${ctx ? ' ' + ctx : ''} | ${mensaje}${kv ? ' | ' + kv : ''}`;
    console.log(linea);
    fs.appendFileSync(archivo, linea + '\n', 'utf8');
  };
  return Object.fromEntries(NIVELES.map(n =>
    [n.toLowerCase(), (m, d) => escribir(n, m, d)]));
}
```

---

## Cómo se lee esto de vuelta

El observador (`sysop-agent`) parsea estos logs para el informe y para el
diagnóstico. El patrón canónico:

```
^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\S*)\s+
 (?P<nivel>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+
 (?P<origen>\S+)\s*
 (?P<ctx>(?:\w+=\S+\s*)*)\|\s*
 (?P<mensaje>[^|]*?)\s*
 (?:\|\s*(?P<datos>.*))?$
```

Y una línea que **no** empieza con marca de tiempo se trata como continuación
del evento anterior. Por eso el traceback indentado funciona.

Mientras haya componentes con el formato viejo, el extractor los tolera: la
convención mejora la precisión, no es un requisito para que el análisis
funcione. Pero cada componente que se adapta es un componente cuyos errores se
pueden **contar** en vez de sólo leer.

---

## Estado de adopción, y qué hay que hacer

| Componente | Dónde se configura | Estado |
|---|---|---|
| `sysop-agent` | `sysop/log_setup.py` | **migrado** (2026-08-28) |
| `workerBot` | `helpers/logger.js` | **migrado**, con rotación |
| `acmeBot` | `helpers/logging_setup.py` | **migrado**, `THREAD_TAG` unificado con `trace` |
| `build` (back-end) | `core/log_setup.py` | **migrado**, con rotación |
| `supervisor` | propio | **no tocar desde el NAS**: su fuente de verdad es srv2 |
| sueltos | ver abajo | pendientes |

### Los sueltos, también migrados

| Archivo | Qué se hizo |
|---|---|
| `acmeBot/shooters/downloadCalls.py` | formato **y rotación**: su log es por cliente y persiste |
| `acmeBot/shooters/leadsReport.py` | el plan B (cuando corre fuera del server) también usa el formato |
| `acmeBot/shooters/maxOrchestrator.py` | idem |
| `acmeBot/rangos/worker.py` | sólo formato: su log es **de una corrida**, no persiste |
| `acmeBot/rangos/syncRango.py` | idem |
| `scripts/bot_dispatch.py` | formato y rotación |
| `scripts/tools_dispatch.py` | formato y rotación |
| `scripts/telemetria/monitor.py` | formato y rotación |

Los dos de `rangos/` conservan su `FileHandler` sin rotar **a propósito**:
escriben un archivo por corrida, dentro del directorio de esa corrida. Rotarlos
sería resolver un problema que no tienen.

### Lo que se aprendió migrando

**No hizo falta tocar un solo llamador.** El workerBot tiene 202 llamadas al
logger y todas escriben el componente adentro del mensaje, entre corchetes:
`log.info('[OPENAI] Conectando...')`. El logger ahora extrae ese prefijo y lo
pone donde la convención lo quiere. Lo que ya se escribía por costumbre resultó
ser el campo que faltaba.

Con una distinción que sí hubo que hacer a mano: `[+]` y `[!!]` **no** son
componentes, son decoración de severidad, y se descartan porque el nivel ya dice
eso y repetirlo ensucia la firma con la que se agrupan los errores. Y `[T 12.3s]`
no es ninguna de las dos cosas —lleva un dato— así que se conserva tal cual.

**El contexto se fija una vez, no en cada llamada.** En el workerBot son cuatro
líneas en `index.js`, una por modo, apenas se sabe el tenant y la línea. De ahí
en más todas las líneas llevan `tenant=`, `media=` y `mode=`, que es lo que hoy
faltaba para saber a quién afecta un error.

**En `acmeBot` el contexto ya estaba y nadie lo había notado.** `setup_logging`
recibe el `client_id` y el nombre del script desde hace tiempo: son exactamente
el `tenant` y el `componente` que la convención pide. Y el `THREAD_TAG` que
comparten un shooter y sus subprocesos ya era una correlación entre procesos —
sólo le faltaba llamarse `trace`. No hubo que tocar ninguno de los 27
llamadores.

**Los dos formatos conviven sin problema.** Verificado sobre el `webapp.log` de
srv3, que hoy tiene líneas viejas y nuevas mezcladas: el extractor las lee todas
y agrupa igual. Migrar de a un componente por vez es seguro.

### El orden que conviene

De menos a más riesgo, no de más a menos ruidoso. La idea es probar el formato
donde equivocarse sale barato.

1. **`sysop-agent`.** Es el que consume los logs y el que menos escribe. Si el
   formato tiene un problema, se descubre acá sin tocar producción.
2. **`workerBot`.** Su logger es un solo archivo de treinta líneas y no tiene
   dependencias. De paso hay que **agregarle rotación**, que hoy no tiene: sus
   archivos son los más grandes del servidor (8 MB sin rotar).
3. **`acmeBot`.** Ya tiene el módulo centralizado y rotación; es cambiar el
   formato en un solo lugar (`config.LOG_FORMAT`) y sumar el contexto. Su
   `THREAD_TAG` ya hace algo parecido a `trace`: conviene unificarlos.
4. **`build`.** El más caro porque primero hay que unificar los cuatro
   `basicConfig` en uno, y es el que está en el camino de cada request.

### Qué se gana en cada paso

No es cosmético y conviene tenerlo a la vista para no dejarlo a mitad de camino:

- **Errores que se pueden contar.** Hoy el observador agrupa por heurística
  —normalizando fechas, ids y números— y funciona bien, pero es una
  aproximación. Con el mensaje separado de los datos, la firma es exacta.
- **Seguir una operación entre componentes.** Con `trace` compartido, un mensaje
  que entra por el canal, pasa por el daemon, dispara una llamada al modelo y
  termina en la base es un `grep`. Hoy es reconstruirlo a mano por
  aproximación de horarios.
- **Saber de quién es el problema.** El formato dominante hoy no lleva el
  tenant. Un error que afecta a un cliente y otro que afecta a los tres se leen
  igual.
- **Rotación en todos lados.** Hoy falta en el workerBot y no hay `logrotate`
  para `/var/log/plataforma` ni `/var/log/clientea`.

### Mientras tanto

El observador **tolera los formatos viejos**: reconoce los cuatro y agrupa igual.
La convención mejora la precisión, no es un requisito para que el análisis
funcione. Cada componente que se adapta es un componente cuyos errores pasan de
leerse a contarse, y se puede hacer de a uno sin coordinar nada.
