-- Ajuste del prompt: que hacer cuando la causa NO esta donde el mapa dice.
--
-- Motivo, medido: se le dio un incidente de un servicio desconocido cuya causa
-- estaba en /var/log/facturacion-nz/procesador.log, una ruta no declarada. El
-- agente pidio el journal de la unidad, listo /var/log/plataforma (la ruta
-- del mapa) y se rindio con confianza media. Nunca listo /var/log, el
-- directorio padre. El mapa lo anclaba a lo declarado.

UPDATE agents SET training_prompt = REPLACE(training_prompt,
'Como usarlas bien:
- Mira primero el `mapa_del_servidor` del paquete: te dice que es cada ruta, que
  vas a encontrar y cuando conviene mirarla. Empeza por ahi antes de explorar.',
'Como usarlas bien:
- Mira primero el `mapa_del_servidor` del paquete: te dice que es cada ruta, que
  vas a encontrar y cuando conviene mirarla. Empeza por ahi antes de explorar.
- **El mapa es una guia, NO un limite.** Lista lo que conocemos, y el servidor
  tiene mas cosas. Si el problema es de un componente que no figura ahi, o si ya
  miraste donde correspondia y no encontraste la causa, SEGUI BUSCANDO:
  lista `/var/log` para ver que otros logs hay, probA el directorio o el archivo
  que se llame como el servicio, mira `/etc/systemd/system` si es una unidad.
  Un log de una aplicacion que no esta declarada es igual de valido que uno que
  si lo esta.
- Antes de responder con confianza media porque "no se encontro la causa",
  preguntate si te queda alguna vuelta y algun lugar obvio sin mirar. Rendirse
  temprano cuesta lo mismo que buscar y sirve menos.')
WHERE agent_id = 'SYSOP' AND client_id = 'acme'
  -- SIN ESTO SE DUPLICA. El texto que busca el REPLACE es un PREFIJO del texto
  -- con el que lo reemplaza, asi que despues de la primera corrida el prompt
  -- CONTIENE lo que se busca: correrlo de nuevo vuelve a matchear e inserta el
  -- bloque una segunda vez. El agente termina con la instruccion repetida y
  -- pagando esos tokens en cada diagnostico.
  AND training_prompt NOT LIKE '%El mapa es una guia, NO un limite.%';
