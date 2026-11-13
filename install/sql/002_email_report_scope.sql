-- El correo del observador se administra desde la pantalla de reportes por email
-- del front, igual que los demas reportes (decision D4).
--
-- Dos cosas que la tabla no tiene hoy y el observador necesita: el scope 'sysop'
-- y el horario. Verificado el 2026-08-27: el ENUM actual es
-- ('department','expenses','angry','dnc') y no hay ninguna columna de hora ni de
-- zona horaria.

ALTER TABLE email_report_recipients
  MODIFY scope ENUM('department','expenses','angry','dnc','sysop') NOT NULL;

-- Horario del informe diario y minuto del digest horario. Nulos para los scopes
-- que no los usan, que son todos menos 'sysop'.
--
-- SE PUEDE CORRER DOS VECES. MySQL no tiene `ADD COLUMN IF NOT EXISTS`, asi que
-- un `ALTER` pelado falla con el error 1060 en la segunda corrida. Eso importa
-- porque el runbook lo presenta como idempotente y porque una instalacion a
-- medias —la primera fallo despues del MODIFY— deja al que la retoma sin poder
-- volver a correr el archivo entero.
--
-- El `MODIFY` de arriba si es idempotente: dejar el ENUM como ya esta no falla.

SET @sql := (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE email_report_recipients
       ADD COLUMN daily_time TIME NULL DEFAULT NULL AFTER lang',
    'DO 0')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'email_report_recipients'
    AND COLUMN_NAME = 'daily_time');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

SET @sql := (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE email_report_recipients
       ADD COLUMN hourly_minute TINYINT NULL DEFAULT NULL AFTER daily_time',
    'DO 0')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'email_report_recipients'
    AND COLUMN_NAME = 'hourly_minute');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

SET @sql := (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE email_report_recipients
       ADD COLUMN timezone VARCHAR(64) NULL DEFAULT NULL AFTER hourly_minute',
    'DO 0')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'email_report_recipients'
    AND COLUMN_NAME = 'timezone');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;
