"""
API SGA Web - Système de Gestion des Agents
Version web complète basée sur gestion_agents.py
"""

from fastapi import FastAPI, HTTPException, Depends, Query, Body, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import sqlite3
import pandas as pd
from datetime import date, datetime, timedelta
from calendar import monthrange
import os
import json
import csv
import tempfile
from contextlib import contextmanager
import logging

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialisation de FastAPI
app = FastAPI(
    title="SGA Web API",
    description="API du Système de Gestion des Agents",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configuration CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # À restreindre en production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Chemins
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_PATH = os.path.join(BASE_DIR, "database", "planning.db")
DATE_AFFECTATION_BASE = "2025-11-01"

# Modèles Pydantic
class AgentBase(BaseModel):
    code: str
    nom: str
    prenom: str
    code_groupe: str

class AgentCreate(AgentBase):
    pass

class AgentResponse(BaseModel):
    code: str
    nom: str
    prenom: str
    code_groupe: str
    date_entree: str
    statut: str

class PlanningRequest(BaseModel):
    mois: int
    annee: int

class ShiftModification(BaseModel):
    code_agent: str
    date: str
    shift: str

class AbsenceRequest(BaseModel):
    code_agent: str
    date: str
    type_absence: str  # C, M, A

class CongeRequest(BaseModel):
    code_agent: str
    date_debut: str
    date_fin: str

class StatsRequest(BaseModel):
    mois: int
    annee: int

# Contexte de connexion à la base
@contextmanager
def get_db_connection():
    """Contexte pour la connexion à la base de données"""
    # S'assurer que le dossier database existe
    db_dir = os.path.dirname(DATABASE_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_db_cursor():
    """Obtenir un curseur de base de données"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        yield cursor, conn

# Initialisation de la base
def init_database():
    """Initialise la base de données avec toutes les tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Table agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                code TEXT PRIMARY KEY,
                nom TEXT NOT NULL,
                prenom TEXT NOT NULL,
                code_groupe TEXT NOT NULL,
                date_entree TEXT,
                date_sortie TEXT,
                statut TEXT DEFAULT 'actif'
            )
        """)
        
        # Table planning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS planning (
                code_agent TEXT,
                date TEXT,
                shift TEXT,
                origine TEXT,
                PRIMARY KEY (code_agent, date)
            )
        """)
        
        # Table jours_feries
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jours_feries (
                date TEXT PRIMARY KEY,
                description TEXT
            )
        """)
        
        # Table codes_panique
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS codes_panique (
                code_agent TEXT PRIMARY KEY,
                code_panique TEXT NOT NULL,
                poste_nom TEXT NOT NULL,
                FOREIGN KEY (code_agent) REFERENCES agents(code)
            )
        """)
        
        # Table radios
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS radios (
                id_radio TEXT PRIMARY KEY,
                modele TEXT NOT NULL,
                statut TEXT NOT NULL
            )
        """)
        
        # Table historique_radio
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS historique_radio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_radio TEXT,
                code_agent TEXT,
                date_attribution TEXT NOT NULL,
                date_retour TEXT,
                FOREIGN KEY (id_radio) REFERENCES radios(id_radio),
                FOREIGN KEY (code_agent) REFERENCES agents(code)
            )
        """)
        
        # Table habillement
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS habillement (
                code_agent TEXT PRIMARY KEY,
                chemise_taille TEXT,
                chemise_date TEXT,
                jacket_taille TEXT,
                jacket_date TEXT,
                pantalon_taille TEXT,
                pantalon_date TEXT,
                cravate_oui TEXT,
                cravate_date TEXT,
                FOREIGN KEY (code_agent) REFERENCES agents(code)
            )
        """)
        
        # Table avertissements
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS avertissements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_agent TEXT,
                date_avertissement TEXT NOT NULL,
                type_avertissement TEXT NOT NULL,
                description TEXT,
                FOREIGN KEY (code_agent) REFERENCES agents(code)
            )
        """)
        
        # Table conges_periode
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conges_periode (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_agent TEXT,
                date_debut TEXT NOT NULL,
                date_fin TEXT NOT NULL,
                date_creation TEXT NOT NULL,
                FOREIGN KEY (code_agent) REFERENCES agents(code)
            )
        """)
        
        conn.commit()
        logger.info("Base de données initialisée avec succès")

# Fonctions utilitaires
JOURS_FRANCAIS = {
    'Mon': 'Lun', 'Tue': 'Mar', 'Wed': 'Mer', 'Thu': 'Jeu',
    'Fri': 'Ven', 'Sat': 'Sam', 'Sun': 'Dim'
}

def _cycle_standard_8j(jour_cycle):
    """Cycle de rotation 8 jours"""
    cycle = ['1', '1', '2', '2', '3', '3', 'R', 'R']
    return cycle[jour_cycle % 8]

def _get_decalage_standard(code_groupe):
    """Décalage par groupe"""
    decalages = {'A': 0, 'B': 2, 'C': 4, 'D': 6}
    return decalages.get(code_groupe.upper(), 0)

def _cycle_c_diff(jour_date: date, code_agent, cursor):
    """Cycle spécial pour le groupe E"""
    jour_semaine = jour_date.weekday()
    
    # Weekend = repos
    if jour_semaine >= 5: 
        return 'R'
    
    cursor.execute("SELECT code FROM agents WHERE code_groupe='E' AND date_sortie IS NULL ORDER BY code")
    agents_du_groupe = [a[0] for a in cursor.fetchall()]
    
    try:
        index_agent = agents_du_groupe.index(code_agent)
    except ValueError:
        return 'R'

    num_semaine = jour_date.isocalendar()[1]
    jour_pair = (jour_semaine % 2 == 0)
    
    if index_agent == 0:
        if num_semaine % 2 != 0: 
            return '1' if jour_pair else '2' 
        else: 
            return '2' if jour_pair else '1'
    
    if index_agent == 1:
        if num_semaine % 2 != 0: 
            return '2' if jour_pair else '1'
        else: 
            return '1' if jour_pair else '2'

    return '1' if (index_agent + num_semaine) % 2 == 0 else '2'

def _est_jour_ferie(date_str: str, cursor) -> bool:
    """Vérifie si une date est fériée"""
    cursor.execute("SELECT 1 FROM jours_feries WHERE date=?", (date_str,))
    if cursor.fetchone():
        return True
    
    # Jours fériés fixes Maroc
    try:
        d = date.fromisoformat(date_str)
        jours_feries_fixes = {
            (1, 1), (1, 11), (5, 1), (7, 30),
            (8, 14), (8, 20), (8, 21), (11, 6), (11, 18)
        }
        return (d.month, d.day) in jours_feries_fixes
    except:
        return False

# Initialiser la base au démarrage
@app.on_event("startup")
async def startup_event():
    """Initialise la base de données au démarrage"""
    init_database()
    logger.info("API SGA Web démarrée")

# =========================================================================
# ENDPOINTS DE BASE
# =========================================================================

@app.get("/")
async def root():
    """Endpoint racine"""
    return {
        "application": "SGA Web API",
        "version": "2.0.0",
        "status": "running",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/health")
async def health_check():
    """Vérifie l'état de l'API et de la base"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            
        return {
            "status": "healthy",
            "database": "connected",
            "tables": len(tables),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# =========================================================================
# GESTION DES AGENTS
# =========================================================================

@app.get("/api/agents", response_model=List[Dict[str, Any]])
async def get_agents(
    groupe: Optional[str] = Query(None, description="Filtrer par groupe"),
    actif: bool = Query(True, description="Agents actifs seulement")
):
    """Récupère la liste des agents"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM agents"
            params = []
            
            if actif:
                query += " WHERE date_sortie IS NULL"
            else:
                query += " WHERE 1=1"
            
            if groupe:
                if "WHERE" in query:
                    query += " AND code_groupe = ?"
                else:
                    query += " WHERE code_groupe = ?"
                params.append(groupe.upper())
            
            query += " ORDER BY code_groupe, code"
            cursor.execute(query, params)
            
            agents = []
            for row in cursor.fetchall():
                agent = dict(row)
                agents.append(agent)
            
            return agents
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/agents/{code_agent}")
async def get_agent(code_agent: str):
    """Récupère un agent spécifique"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM agents WHERE code = ?", (code_agent.upper(),))
            agent = cursor.fetchone()
            
            if not agent:
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            return dict(agent)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/agents")
async def create_agent(agent: AgentCreate):
    """Ajoute un nouvel agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier si l'agent existe déjà
            cursor.execute("SELECT code FROM agents WHERE code = ?", (agent.code.upper(),))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail=f"L'agent {agent.code} existe déjà")
            
            # Insérer le nouvel agent
            cursor.execute("""
                INSERT INTO agents (code, nom, prenom, code_groupe, date_entree)
                VALUES (?, ?, ?, ?, ?)
            """, (
                agent.code.upper(),
                agent.nom,
                agent.prenom,
                agent.code_groupe.upper(),
                DATE_AFFECTATION_BASE
            ))
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Agent {agent.code} créé avec succès",
                "agent": {
                    "code": agent.code.upper(),
                    "nom": agent.nom,
                    "prenom": agent.prenom,
                    "groupe": agent.code_groupe.upper()
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/agents/{code_agent}")
async def update_agent(
    code_agent: str,
    nom: Optional[str] = Form(None),
    prenom: Optional[str] = Form(None),
    code_groupe: Optional[str] = Form(None)
):
    """Met à jour un agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier que l'agent existe
            cursor.execute("SELECT * FROM agents WHERE code = ?", (code_agent.upper(),))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            # Construire la requête de mise à jour
            updates = []
            params = []
            
            if nom:
                updates.append("nom = ?")
                params.append(nom)
            if prenom:
                updates.append("prenom = ?")
                params.append(prenom)
            if code_groupe:
                if code_groupe.upper() not in ['A', 'B', 'C', 'D', 'E']:
                    raise HTTPException(status_code=400, detail="Groupe invalide")
                updates.append("code_groupe = ?")
                params.append(code_groupe.upper())
            
            if not updates:
                raise HTTPException(status_code=400, detail="Aucune donnée à mettre à jour")
            
            params.append(code_agent.upper())
            query = f"UPDATE agents SET {', '.join(updates)} WHERE code = ?"
            cursor.execute(query, params)
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Agent {code_agent} mis à jour"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/agents/{code_agent}")
async def delete_agent(code_agent: str):
    """Marque un agent comme inactif"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            date_sortie = date.today().isoformat()
            cursor.execute("""
                UPDATE agents 
                SET date_sortie = ?, statut = 'inactif'
                WHERE code = ? AND date_sortie IS NULL
            """, (date_sortie, code_agent.upper()))
            
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Agent non trouvé ou déjà inactif")
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Agent {code_agent} marqué comme inactif"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# PLANNING
# =========================================================================

def _get_shift_theorique(code_agent: str, jour_date: date, cursor):
    """Calcule le shift théorique pour un agent à une date"""
    cursor.execute("SELECT code_groupe, date_entree FROM agents WHERE code = ?", (code_agent,))
    agent_info = cursor.fetchone()
    
    if not agent_info:
        return '-'
    
    code_groupe, date_entree_str = agent_info
    
    if code_groupe == 'E':
        return _cycle_c_diff(jour_date, code_agent, cursor)
    
    elif code_groupe in ['A', 'B', 'C', 'D']:
        date_entree = date.fromisoformat(date_entree_str)
        delta_jours = (jour_date - date_entree).days
        decalage = _get_decalage_standard(code_groupe)
        jour_cycle = delta_jours + decalage
        return _cycle_standard_8j(jour_cycle)
    
    return 'R'

def _get_shift_effectif(code_agent: str, date_str: str, cursor, conn):
    """Récupère ou calcule le shift pour un agent à une date"""
    cursor.execute("SELECT shift FROM planning WHERE code_agent = ? AND date = ?", (code_agent, date_str))
    result = cursor.fetchone()
    
    if result:
        return result[0]
    
    # Calculer le shift théorique
    date_obj = date.fromisoformat(date_str)
    shift_theorique = _get_shift_theorique(code_agent, date_obj, cursor)
    
    if shift_theorique != '-':
        cursor.execute("""
            INSERT OR REPLACE INTO planning (code_agent, date, shift, origine)
            VALUES (?, ?, ?, 'THEORIQUE')
        """, (code_agent, date_str, shift_theorique))
        conn.commit()
    
    return shift_theorique

@app.get("/api/planning/global/{mois}/{annee}")
async def get_planning_global(mois: int, annee: int):
    """Récupère le planning global pour un mois"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier les paramètres
            if not (1 <= mois <= 12):
                raise HTTPException(status_code=400, detail="Mois invalide (1-12)")
            
            # Nombre de jours dans le mois
            _, jours_mois = monthrange(annee, mois)
            
            # Récupérer les agents actifs
            cursor.execute("""
                SELECT code, nom, prenom, code_groupe 
                FROM agents 
                WHERE date_sortie IS NULL 
                ORDER BY code_groupe, code
            """)
            agents = cursor.fetchall()
            
            # Préparer les données
            planning_data = []
            jours_info = []
            
            # Informations sur les jours
            for jour in range(1, jours_mois + 1):
                jour_date = date(annee, mois, jour)
                date_str = jour_date.isoformat()
                
                jours_info.append({
                    "numero": jour,
                    "date": date_str,
                    "jour_semaine": JOURS_FRANCAIS[jour_date.strftime('%a')],
                    "ferie": _est_jour_ferie(date_str, cursor)
                })
            
            # Planning par agent
            for agent in agents:
                code, nom, prenom, groupe = agent
                agent_shifts = []
                
                for jour in range(1, jours_mois + 1):
                    date_str = date(annee, mois, jour).isoformat()
                    shift = _get_shift_effectif(code, date_str, cursor, conn)
                    agent_shifts.append(shift)
                
                planning_data.append({
                    "code": code,
                    "nom": nom,
                    "prenom": prenom,
                    "groupe": groupe,
                    "nom_complet": f"{nom} {prenom}",
                    "shifts": agent_shifts
                })
            
            return {
                "mois": mois,
                "annee": annee,
                "total_jours": jours_mois,
                "total_agents": len(agents),
                "jours": jours_info,
                "agents": planning_data
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/planning/groupe/{groupe}/{mois}/{annee}")
async def get_planning_groupe(groupe: str, mois: int, annee: int):
    """Récupère le planning d'un groupe spécifique"""
    try:
        groupe = groupe.upper()
        if groupe not in ['A', 'B', 'C', 'D', 'E']:
            raise HTTPException(status_code=400, detail="Groupe invalide")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Récupérer les agents du groupe
            cursor.execute("""
                SELECT code, nom, prenom 
                FROM agents 
                WHERE code_groupe = ? AND date_sortie IS NULL 
                ORDER BY code
            """, (groupe,))
            agents = cursor.fetchall()
            
            if not agents:
                raise HTTPException(status_code=404, detail=f"Aucun agent actif dans le groupe {groupe}")
            
            # Nombre de jours dans le mois
            _, jours_mois = monthrange(annee, mois)
            
            # Préparer les données
            planning_data = []
            jours_info = []
            
            # Informations sur les jours
            for jour in range(1, jours_mois + 1):
                jour_date = date(annee, mois, jour)
                date_str = jour_date.isoformat()
                
                jours_info.append({
                    "numero": jour,
                    "date": date_str,
                    "jour_semaine": JOURS_FRANCAIS[jour_date.strftime('%a')],
                    "ferie": _est_jour_ferie(date_str, cursor)
                })
            
            # Planning par agent
            for agent in agents:
                code, nom, prenom = agent
                agent_shifts = []
                
                for jour in range(1, jours_mois + 1):
                    date_str = date(annee, mois, jour).isoformat()
                    shift = _get_shift_effectif(code, date_str, cursor, conn)
                    agent_shifts.append(shift)
                
                planning_data.append({
                    "code": code,
                    "nom": nom,
                    "prenom": prenom,
                    "nom_complet": f"{nom} {prenom}",
                    "shifts": agent_shifts
                })
            
            return {
                "groupe": groupe,
                "mois": mois,
                "annee": annee,
                "total_jours": jours_mois,
                "total_agents": len(agents),
                "jours": jours_info,
                "agents": planning_data
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/planning/agent/{code_agent}/{mois}/{annee}")
async def get_planning_agent(code_agent: str, mois: int, annee: int):
    """Récupère le planning d'un agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier l'agent
            cursor.execute("""
                SELECT nom, prenom, code_groupe 
                FROM agents 
                WHERE code = ?
            """, (code_agent.upper(),))
            agent_info = cursor.fetchone()
            
            if not agent_info:
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            nom, prenom, groupe = agent_info
            
            # Nombre de jours dans le mois
            _, jours_mois = monthrange(annee, mois)
            
            # Planning quotidien
            planning_jours = []
            
            for jour in range(1, jours_mois + 1):
                jour_date = date(annee, mois, jour)
                date_str = jour_date.isoformat()
                shift = _get_shift_effectif(code_agent.upper(), date_str, cursor, conn)
                
                planning_jours.append({
                    "jour_numero": jour,
                    "date": date_str,
                    "jour_semaine": JOURS_FRANCAIS[jour_date.strftime('%a')],
                    "shift": shift,
                    "ferie": _est_jour_ferie(date_str, cursor)
                })
            
            return {
                "agent": {
                    "code": code_agent.upper(),
                    "nom": nom,
                    "prenom": prenom,
                    "groupe": groupe,
                    "nom_complet": f"{nom} {prenom}"
                },
                "mois": mois,
                "annee": annee,
                "total_jours": jours_mois,
                "planning": planning_jours
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/planning/modifier")
async def modifier_shift(data: ShiftModification):
    """Modifie un shift manuellement"""
    try:
        if data.shift.upper() not in ['1', '2', '3', 'R', 'C', 'M', 'A']:
            raise HTTPException(status_code=400, detail="Shift invalide")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier l'agent
            cursor.execute("SELECT code FROM agents WHERE code = ?", (data.code_agent.upper(),))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            # Modifier le shift
            cursor.execute("""
                INSERT OR REPLACE INTO planning (code_agent, date, shift, origine)
                VALUES (?, ?, ?, 'MANUEL')
            """, (data.code_agent.upper(), data.date, data.shift.upper()))
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Shift modifié pour {data.code_agent} le {data.date}"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/planning/absence")
async def enregistrer_absence(data: AbsenceRequest):
    """Enregistre une absence"""
    try:
        if data.type_absence.upper() not in ['C', 'M', 'A']:
            raise HTTPException(status_code=400, detail="Type d'absence invalide")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier l'agent
            cursor.execute("SELECT code FROM agents WHERE code = ?", (data.code_agent.upper(),))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            # Enregistrer l'absence
            cursor.execute("""
                INSERT OR REPLACE INTO planning (code_agent, date, shift, origine)
                VALUES (?, ?, ?, 'ABSENCE')
            """, (data.code_agent.upper(), data.date, data.type_absence.upper()))
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Absence enregistrée pour {data.code_agent} le {data.date}"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# STATISTIQUES
# =========================================================================

def _calculer_stats_agent(code_agent: str, mois: int, annee: int, cursor):
    """Calcule les statistiques d'un agent"""
    _, jours_mois = monthrange(annee, mois)
    
    # Initialiser les compteurs
    stats = {
        '1': 0, '2': 0, '3': 0, 'R': 0, 'C': 0, 'M': 0, 'A': 0, '-': 0
    }
    feries_travailles = 0
    
    # Récupérer le groupe
    cursor.execute("SELECT code_groupe FROM agents WHERE code = ?", (code_agent,))
    result = cursor.fetchone()
    code_groupe = result[0] if result else None
    
    # Calculer pour chaque jour
    date_debut = date(annee, mois, 1).isoformat()
    date_fin = date(annee, mois, jours_mois).isoformat()
    
    cursor.execute("""
        SELECT shift, date FROM planning 
        WHERE code_agent = ? AND date BETWEEN ? AND ?
    """, (code_agent, date_debut, date_fin))
    
    for shift, date_str in cursor.fetchall():
        if shift in stats:
            stats[shift] += 1
            
            # Compter les fériés travaillés
            if shift in ['1', '2', '3'] and _est_jour_ferie(date_str, cursor):
                feries_travailles += 1
    
    # Calculer les totaux
    total_shifts = stats['1'] + stats['2'] + stats['3']
    
    if code_groupe == 'E':
        total_operationnels = total_shifts
    else:
        total_operationnels = total_shifts + feries_travailles
    
    return {
        'stats': stats,
        'feries_travailles': feries_travailles,
        'total_shifts': total_shifts,
        'total_operationnels': total_operationnels,
        'groupe': code_groupe
    }

@app.get("/api/stats/agent/{code_agent}/{mois}/{annee}")
async def get_stats_agent(code_agent: str, mois: int, annee: int):
    """Statistiques d'un agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier l'agent
            cursor.execute("SELECT nom, prenom, code_groupe FROM agents WHERE code = ?", (code_agent.upper(),))
            agent_info = cursor.fetchone()
            
            if not agent_info:
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            nom, prenom, groupe = agent_info
            
            # Calculer les statistiques
            resultats = _calculer_stats_agent(code_agent.upper(), mois, annee, cursor)
            
            # Formater la réponse
            statistiques = [
                {"description": "Shifts Matin (1)", "valeur": resultats['stats']['1']},
                {"description": "Shifts Après-midi (2)", "valeur": resultats['stats']['2']},
                {"description": "Shifts Nuit (3)", "valeur": resultats['stats']['3']},
                {"description": "Jours Repos (R)", "valeur": resultats['stats']['R']},
                {"description": "Congés (C)", "valeur": resultats['stats']['C']},
                {"description": "Maladie (M)", "valeur": resultats['stats']['M']},
                {"description": "Autre Absence (A)", "valeur": resultats['stats']['A']},
                {"description": "Fériés travaillés", "valeur": resultats['feries_travailles']},
                {"description": "Non-planifié (-)", "valeur": resultats['stats']['-']},
                {"description": "TOTAL SHIFTS OPÉRATIONNELS", "valeur": resultats['total_operationnels']}
            ]
            
            return {
                "agent": {
                    "code": code_agent.upper(),
                    "nom": nom,
                    "prenom": prenom,
                    "groupe": groupe
                },
                "mois": mois,
                "annee": annee,
                "statistiques": statistiques,
                "total_operationnels": resultats['total_operationnels']
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats/global/{mois}/{annee}")
async def get_stats_global(mois: int, annee: int):
    """Statistiques globales"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Récupérer tous les agents actifs
            cursor.execute("SELECT code FROM agents WHERE date_sortie IS NULL")
            agents = [row[0] for row in cursor.fetchall()]
            
            # Initialiser les totaux
            stats_globales = {
                '1': 0, '2': 0, '3': 0, 'R': 0, 'C': 0, 'M': 0, 'A': 0, '-': 0
            }
            total_feries = 0
            total_operationnels = 0
            
            # Agréger les statistiques
            for agent in agents:
                try:
                    resultats = _calculer_stats_agent(agent, mois, annee, cursor)
                    
                    for key in stats_globales:
                        stats_globales[key] += resultats['stats'][key]
                    
                    total_feries += resultats['feries_travailles']
                    total_operationnels += resultats['total_operationnels']
                except:
                    continue
            
            # Compter les agents par groupe
            cursor.execute("""
                SELECT code_groupe, COUNT(*) 
                FROM agents 
                WHERE date_sortie IS NULL 
                GROUP BY code_groupe
            """)
            groupes = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Formater la réponse
            statistiques = [
                {"description": "Shifts Matin (1)", "valeur": stats_globales['1']},
                {"description": "Shifts Après-midi (2)", "valeur": stats_globales['2']},
                {"description": "Shifts Nuit (3)", "valeur": stats_globales['3']},
                {"description": "Jours Repos (R)", "valeur": stats_globales['R']},
                {"description": "Congés (C)", "valeur": stats_globales['C']},
                {"description": "Maladie (M)", "valeur": stats_globales['M']},
                {"description": "Autre Absence (A)", "valeur": stats_globales['A']},
                {"description": "Fériés travaillés", "valeur": total_feries},
                {"description": "Non-planifié (-)", "valeur": stats_globales['-']},
                {"description": "TOTAL SHIFTS OPÉRATIONNELS", "valeur": total_operationnels}
            ]
            
            return {
                "mois": mois,
                "annee": annee,
                "statistiques": statistiques,
                "total_agents": len(agents),
                "groupes": groupes,
                "total_operationnels": total_operationnels
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats/jours-travailles/{mois}/{annee}")
async def get_jours_travailles(
    mois: int, 
    annee: int,
    groupe: Optional[str] = None
):
    """Jours travaillés par groupe ou global"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            if groupe:
                # Par groupe spécifique
                groupe = groupe.upper()
                cursor.execute("""
                    SELECT code, nom, prenom 
                    FROM agents 
                    WHERE code_groupe = ? AND date_sortie IS NULL 
                    ORDER BY code
                """, (groupe,))
                agents = cursor.fetchall()
                
                if not agents:
                    raise HTTPException(status_code=404, detail=f"Aucun agent dans le groupe {groupe}")
                
                resultats = []
                total_groupe = 0
                
                for code, nom, prenom in agents:
                    resultats_agent = _calculer_stats_agent(code, mois, annee, cursor)
                    jours_travailles = resultats_agent['stats']['1'] + resultats_agent['stats']['2'] + resultats_agent['stats']['3']
                    
                    resultats.append({
                        "code": code,
                        "nom": nom,
                        "prenom": prenom,
                        "jours_travailles": jours_travailles
                    })
                    total_groupe += jours_travailles
                
                return {
                    "groupe": groupe,
                    "mois": mois,
                    "annee": annee,
                    "agents": resultats,
                    "total_groupe": total_groupe,
                    "moyenne": total_groupe / len(agents) if agents else 0
                }
            else:
                # Tous les groupes
                groupes = ['A', 'B', 'C', 'D', 'E']
                resultats = []
                total_global = 0
                total_agents = 0
                
                for grp in groupes:
                    cursor.execute("""
                        SELECT code FROM agents 
                        WHERE code_groupe = ? AND date_sortie IS NULL
                    """, (grp,))
                    agents_groupe = [row[0] for row in cursor.fetchall()]
                    
                    if agents_groupe:
                        total_groupe = 0
                        for agent in agents_groupe:
                            resultats_agent = _calculer_stats_agent(agent, mois, annee, cursor)
                            total_groupe += resultats_agent['stats']['1'] + resultats_agent['stats']['2'] + resultats_agent['stats']['3']
                        
                        resultats.append({
                            "groupe": grp,
                            "nombre_agents": len(agents_groupe),
                            "total_jours": total_groupe,
                            "moyenne": total_groupe / len(agents_groupe) if agents_groupe else 0
                        })
                        
                        total_global += total_groupe
                        total_agents += len(agents_groupe)
                
                return {
                    "mois": mois,
                    "annee": annee,
                    "groupes": resultats,
                    "total_global": total_global,
                    "total_agents": total_agents,
                    "moyenne_globale": total_global / total_agents if total_agents else 0
                }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# TABLEAU DE BORD
# =========================================================================

@app.get("/api/dashboard")
async def get_dashboard():
    """Statistiques pour le tableau de bord"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Agents par groupe
            cursor.execute("""
                SELECT code_groupe, COUNT(*) 
                FROM agents 
                WHERE date_sortie IS NULL 
                GROUP BY code_groupe
            """)
            groupes = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Présents aujourd'hui
            aujourdhui = date.today().isoformat()
            cursor.execute("""
                SELECT COUNT(DISTINCT code_agent) 
                FROM planning 
                WHERE date = ? AND shift IN ('1', '2', '3')
            """, (aujourdhui,))
            presents = cursor.fetchone()[0] or 0
            
            # Congés aujourd'hui
            cursor.execute("""
                SELECT COUNT(DISTINCT code_agent) 
                FROM planning 
                WHERE date = ? AND shift = 'C'
            """, (aujourdhui,))
            conges = cursor.fetchone()[0] or 0
            
            # Agents inactifs récents (30 derniers jours)
            date_limite = (date.today() - timedelta(days=30)).isoformat()
            cursor.execute("""
                SELECT COUNT(*) 
                FROM agents 
                WHERE date_sortie >= ?
            """, (date_limite,))
            inactifs_recents = cursor.fetchone()[0] or 0
            
            # Radios disponibles
            cursor.execute("SELECT COUNT(*) FROM radios WHERE statut = 'DISPONIBLE'")
            radios_disponibles = cursor.fetchone()[0] or 0
            
            # Avertissements récents (7 derniers jours)
            date_limite_avert = (date.today() - timedelta(days=7)).isoformat()
            cursor.execute("""
                SELECT COUNT(*) 
                FROM avertissements 
                WHERE date_avertissement >= ?
            """, (date_limite_avert,))
            avertissements_recents = cursor.fetchone()[0] or 0
            
            return {
                "date": aujourdhui,
                "total_agents": sum(groupes.values()),
                "agents_par_groupe": groupes,
                "presents_aujourdhui": presents,
                "conges_aujourdhui": conges,
                "inactifs_recents": inactifs_recents,
                "radios_disponibles": radios_disponibles,
                "avertissements_recents": avertissements_recents,
                "status": "success"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# IMPORT/EXPORT
# =========================================================================

@app.post("/api/import/excel")
async def import_excel(file: UploadFile = File(...)):
    """Importe des agents depuis un fichier Excel"""
    try:
        # Lire le fichier Excel
        contents = await file.read()
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
        
        try:
            df = pd.read_excel(tmp_path)
        except Exception as e:
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail=f"Erreur lecture Excel: {str(e)}")
        
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        
        # Traiter les données
        resultats = {
            "importes": 0,
            "ignores": 0,
            "erreurs": []
        }
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            for index, row in df.iterrows():
                try:
                    code = str(row.iloc[0]).strip().upper() if pd.notna(row.iloc[0]) else ""
                    nom = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
                    prenom = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""
                    groupe = str(row.iloc[3]).strip().upper() if pd.notna(row.iloc[3]) else ""
                    
                    if not code or not nom or not prenom or groupe not in ['A', 'B', 'C', 'D', 'E']:
                        resultats["ignores"] += 1
                        continue
                    
                    # Vérifier si l'agent existe
                    cursor.execute("SELECT code FROM agents WHERE code = ?", (code,))
                    
                    if cursor.fetchone():
                        # Mettre à jour
                        cursor.execute("""
                            UPDATE agents 
                            SET nom = ?, prenom = ?, code_groupe = ?, date_sortie = NULL 
                            WHERE code = ?
                        """, (nom, prenom, groupe, code))
                    else:
                        # Insérer
                        cursor.execute("""
                            INSERT INTO agents (code, nom, prenom, code_groupe, date_entree)
                            VALUES (?, ?, ?, ?, ?)
                        """, (code, nom, prenom, groupe, DATE_AFFECTATION_BASE))
                    
                    resultats["importes"] += 1
                    
                except Exception as e:
                    resultats["erreurs"].append(f"Ligne {index+1}: {str(e)}")
                    resultats["ignores"] += 1
            
            conn.commit()
        
        return resultats
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/export/stats/{mois}/{annee}")
async def export_stats(mois: int, annee: int):
    """Exporte les statistiques en CSV"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Récupérer les agents
            cursor.execute("""
                SELECT code, nom, prenom, code_groupe 
                FROM agents 
                WHERE date_sortie IS NULL 
                ORDER BY code_groupe, code
            """)
            agents = cursor.fetchall()
            
            # Préparer les données CSV
            csv_data = []
            headers = ["Code", "Nom", "Prénom", "Groupe", "Matin (1)", "Après-midi (2)", "Nuit (3)", "Repos (R)", 
                      "Congés (C)", "Maladie (M)", "Autre (A)", "Fériés", "TOTAL"]
            
            csv_data.append(headers)
            
            for agent in agents:
                code, nom, prenom, groupe = agent
                resultats = _calculer_stats_agent(code, mois, annee, cursor)
                
                row = [
                    code, nom, prenom, groupe,
                    resultats['stats']['1'],
                    resultats['stats']['2'],
                    resultats['stats']['3'],
                    resultats['stats']['R'],
                    resultats['stats']['C'],
                    resultats['stats']['M'],
                    resultats['stats']['A'],
                    resultats['feries_travailles'],
                    resultats['total_operationnels']
                ]
                
                csv_data.append(row)
            
            # Créer le fichier CSV temporaire
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as tmp:
                writer = csv.writer(tmp, delimiter=';')
                writer.writerows(csv_data)
                tmp_path = tmp.name
            
            # Retourner le fichier
            return FileResponse(
                tmp_path,
                media_type='text/csv',
                filename=f"stats_{mois:02d}_{annee}.csv"
            )
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# FONCTIONNALITÉS ANNEXES (RADIOS, CONGÉS, AVERTISSEMENTS, etc.)
# =========================================================================

@app.get("/api/radios")
async def get_radios():
    """Récupère l'état des radios"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT r.id_radio, r.modele, r.statut, 
                       a.code as code_agent, a.nom, a.prenom
                FROM radios r
                LEFT JOIN historique_radio h ON r.id_radio = h.id_radio AND h.date_retour IS NULL
                LEFT JOIN agents a ON h.code_agent = a.code
                ORDER BY r.id_radio
            """)
            
            radios = []
            for row in cursor.fetchall():
                radio = dict(row)
                if radio['code_agent']:
                    radio['attribue_a'] = f"{radio['prenom']} {radio['nom']} ({radio['code_agent']})"
                else:
                    radio['attribue_a'] = None
                radios.append(radio)
            
            # Statistiques
            cursor.execute("SELECT statut, COUNT(*) FROM radios GROUP BY statut")
            stats = {row[0]: row[1] for row in cursor.fetchall()}
            
            return {
                "radios": radios,
                "statistiques": stats,
                "total": len(radios)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conges/agent/{code_agent}")
async def get_conges_agent(code_agent: str):
    """Récupère les congés d'un agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT date_debut, date_fin, date_creation 
                FROM conges_periode 
                WHERE code_agent = ? 
                ORDER BY date_debut
            """, (code_agent.upper(),))
            
            conges = []
            for debut, fin, creation in cursor.fetchall():
                debut_obj = date.fromisoformat(debut)
                fin_obj = date.fromisoformat(fin)
                duree = (fin_obj - debut_obj).days + 1
                
                conges.append({
                    "debut": debut,
                    "fin": fin,
                    "duree": duree,
                    "creation": creation
                })
            
            return {
                "agent": code_agent.upper(),
                "conges": conges,
                "total": len(conges)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/conges")
async def ajouter_conge(data: CongeRequest):
    """Ajoute une période de congé"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier l'agent
            cursor.execute("SELECT code FROM agents WHERE code = ?", (data.code_agent.upper(),))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Agent non trouvé")
            
            # Vérifier les dates
            debut_obj = date.fromisoformat(data.date_debut)
            fin_obj = date.fromisoformat(data.date_fin)
            
            if debut_obj > fin_obj:
                raise HTTPException(status_code=400, detail="Date début > Date fin")
            
            # Enregistrer la période
            date_creation = date.today().isoformat()
            cursor.execute("""
                INSERT INTO conges_periode (code_agent, date_debut, date_fin, date_creation)
                VALUES (?, ?, ?, ?)
            """, (data.code_agent.upper(), data.date_debut, data.date_fin, date_creation))
            
            # Appliquer les congés jour par jour
            current_date = debut_obj
            jours_conges = 0
            
            while current_date <= fin_obj:
                date_str = current_date.isoformat()
                
                # Dimanche = repos forcé
                if current_date.weekday() == 6:
                    cursor.execute("""
                        INSERT OR REPLACE INTO planning (code_agent, date, shift, origine)
                        VALUES (?, ?, ?, 'CONGE_DIMANCHE')
                    """, (data.code_agent.upper(), date_str, 'R'))
                else:
                    cursor.execute("""
                        INSERT OR REPLACE INTO planning (code_agent, date, shift, origine)
                        VALUES (?, ?, ?, 'CONGE_PERIODE')
                    """, (data.code_agent.upper(), date_str, 'C'))
                    jours_conges += 1
                
                current_date += timedelta(days=1)
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Congé ajouté pour {data.code_agent}",
                "jours_conges": jours_conges,
                "duree_totale": (fin_obj - debut_obj).days + 1
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# CONFIGURATION ET UTILITAIRES
# =========================================================================

@app.get("/api/config")
async def get_config():
    """Récupère la configuration de l'application"""
    return {
        "nom": "SGA Web",
        "version": "2.0.0",
        "date_affectation_base": DATE_AFFECTATION_BASE,
        "groupes": ["A", "B", "C", "D", "E"],
        "shifts": ["1", "2", "3", "R", "C", "M", "A"],
        "database": os.path.basename(DATABASE_PATH),
        "database_size": os.path.getsize(DATABASE_PATH) if os.path.exists(DATABASE_PATH) else 0,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=2, description="Terme de recherche"),
    type: str = Query("all", description="Type de recherche: agents, planning, stats")
):
    """Recherche globale"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            results = []
            
            if type in ["all", "agents"]:
                cursor.execute("""
                    SELECT code, nom, prenom, code_groupe 
                    FROM agents 
                    WHERE (code LIKE ? OR nom LIKE ? OR prenom LIKE ?)
                    AND date_sortie IS NULL
                    LIMIT 20
                """, (f"%{q}%", f"%{q}%", f"%{q}%"))
                
                for row in cursor.fetchall():
                    results.append({
                        "type": "agent",
                        "code": row[0],
                        "nom": f"{row[2]} {row[1]}",
                        "groupe": row[3],
                        "details": f"Groupe {row[3]}"
                    })
            
            return {
                "query": q,
                "type": type,
                "results": results,
                "count": len(results)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# =========================================================================
# INTERFACE WEB
# =========================================================================

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

# Configuration des dossiers
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Page d'accueil avec interface web
@app.get("/")
async def home(request: Request):
    """Page d'accueil avec interface web"""
    return templates.TemplateResponse("index.html", {"request": request})

# =========================================================================
# LANCEMENT DE L'APPLICATION
# ============================================================

if __name__ == "__main__":
    import uvicorn
    
    print("=" * 60)
    print("🚀 SGA Web API - Système de Gestion des Agents")
    print("=" * 60)
    print(f"📁 Base de données: {DATABASE_PATH}")
    print(f"🌐 API disponible sur: http://localhost:8000")
    print(f"📚 Documentation: http://localhost:8000/docs")
    print(f"🔍 Redoc: http://localhost:8000/redoc")
    print("=" * 60)
    print("📊 Endpoints principaux:")
    print("  - GET  /api/agents            → Liste des agents")
    print("  - GET  /api/dashboard         → Tableau de bord")
    print("  - GET  /api/planning/global/{mois}/{annee} → Planning global")
    print("  - GET  /api/stats/agent/{code}/{mois}/{annee} → Statistiques agent")
    print("  - POST /api/import/excel      → Importer agents Excel")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=True  # Recharge automatiquement lors des changements
)
