-- Usuario de monitoreo del observador.
--
-- PROCESS y SELECT sobre performance_schema NO son opcionales, contra lo que
-- sugiere la seccion 9.2 de la especificacion. Verificado el 2026-08-27 contra
-- srv2 con un usuario de solo SELECT: performance_schema da error 1142 e
-- information_schema.processlist devuelve 1 fila de las 232 conexiones reales.
-- Sin esto, cinco controles de base de datos son imposibles (DB-03 a DB-08).
--
-- Reemplazar <SECRETO> antes de correr. El valor va a /etc/sysop-agent/sysop-agent.env,
-- nunca a este archivo ni a git.

-- DOS HOSTS CONCRETOS, NUNCA '%'. El observador corre en la misma maquina que
-- la base, asi que no necesita entrar desde ningun otro lado. Son dos porque en
-- MySQL 'localhost' matchea el socket y el observador se conecta por TCP a
-- 127.0.0.1 — es lo mismo que hace `install.sh`, que ya lo tenia bien.
--
-- Con '%' este usuario se podia usar desde cualquier IP, y el MySQL de
-- produccion esta publicado hacia afuera: un SELECT sobre toda la base mas
-- PROCESS, alcanzable desde internet con solo tener la password.

CREATE USER IF NOT EXISTS 'sysop_ro'@'localhost' IDENTIFIED BY '<SECRETO>';
CREATE USER IF NOT EXISTS 'sysop_ro'@'127.0.0.1' IDENTIFIED BY '<SECRETO>';

GRANT SELECT ON plataforma.*      TO 'sysop_ro'@'localhost';
GRANT SELECT ON performance_schema.*  TO 'sysop_ro'@'localhost';
GRANT PROCESS ON *.*                  TO 'sysop_ro'@'localhost';

GRANT SELECT ON plataforma.*      TO 'sysop_ro'@'127.0.0.1';
GRANT SELECT ON performance_schema.*  TO 'sysop_ro'@'127.0.0.1';
GRANT PROCESS ON *.*                  TO 'sysop_ro'@'127.0.0.1';

-- Si una instalacion vieja creo el usuario con comodin, esto lo saca. Sin el
-- DROP, arreglar el archivo no arregla la base que ya lo tiene.
DROP USER IF EXISTS 'sysop_ro'@'%';

-- Deliberadamente NO se otorgan: INSERT, UPDATE, DELETE, FILE, SUPER,
-- SHUTDOWN, GRANT OPTION ni permisos sobre mysql.*.

FLUSH PRIVILEGES;
