"""
Configuration de l'application SGA Web
"""

import os
from pathlib import Path

# Chemins de base
BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "planning.db"

# Constantes
DATE_AFFECTATION_BASE = "2025-11-01"
JOURS_FRANCAIS = {
    'Mon': 'Lun', 'Tue': 'Mar', 'Wed': 'Mer', 'Thu': 'Jeu',
    'Fri': 'Ven', 'Sat': 'Sam', 'Sun': 'Dim'
}

# Configuration API
API_CONFIG = {
    "title": "SGA Web API",
    "description": "API du Système de Gestion des Agents",
    "version": "2.0.0",
    "docs_url": "/docs",
    "redoc_url": "/redoc",
    "host": "0.0.0.0",
    "port": 8000,
    "reload": True
}

# Assurer que les dossiers existent
def init_directories():
    """Initialise les répertoires nécessaires"""
    directories = [DATABASE_DIR]
    
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    
    return True
