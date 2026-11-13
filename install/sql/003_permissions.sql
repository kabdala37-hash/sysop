-- Permisos del panel del observador. Misma familia que platform.email_reports.manage
-- y platform.sip.manage, que ya existen con categoria 'Platform'.

INSERT IGNORE INTO permissions (permiId, description, category) VALUES
  ('platform.sysop.view',
   'Ver el panel del observador, sus incidentes y su evidencia',
   'Platform'),
  ('platform.sysop.manage',
   'Gestionar expectativas, silencios y ventanas de mantenimiento del observador',
   'Platform');
