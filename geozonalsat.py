from __future__ import annotations

from qgis.PyQt.QtWidgets import QAction

from .geozonalsat_dialog import GeoZonalSatDialog


class GeoZonalSat:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.menu_name = "&GeoZonalSat"

    def initGui(self):
        self.action = QAction("GeoZonalSat", self.iface.mainWindow())
        self.action.setObjectName("GeoZonalSatAction")
        self.action.setToolTip("GeoZonalSat - estatisticas zonais de indices espectrais")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.menu_name, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(self.menu_name, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def run(self):
        if self.dialog is None:
            self.dialog = GeoZonalSatDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

