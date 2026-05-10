# ======================================================
# SITE ADMIN  (/admin)
# Superadmin-only dashboard with site-wide stats,
# user management, and league management.
# ======================================================

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user=Depends(get_current_user)):
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403, detail="Superadmin only")

    conn = get_db()

    # ── Site-wide counts ──────────────────────────────
    total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_leagues = conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0]
    total_teams   = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    total_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    total_picks   = conn.execute("SELECT COUNT(*) FROM team_roster WHERE is_pony=0").fetchone()[0]
    total_ponies  = conn.execute("SELECT COUNT(*) FROM team_roster WHERE is_pony=1").fetchone()[0]
    total_scores  = conn.execute("SELECT COUNT(*) FROM player_scores").fetchone()[0]
    total_audits  = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    drafts_complete = conn.execute("SELECT COUNT(*) FROM draft_state WHERE is_complete=1").fetchone()[0]
    drafts_active   = conn.execute("SELECT COUNT(*) FROM draft_state WHERE is_complete=0").fetchone()[0]

    # ── All users ────────────────────────────────────
    users = conn.execute("""
        SELECT u.id, u.username, u.is_superadmin,
               COUNT(DISTINCT lm.league_id) AS league_count,
               COUNT(DISTINCT t.id)         AS team_count
        FROM users u
        LEFT JOIN league_members lm ON lm.user_id = u.id
        LEFT JOIN teams t           ON t.owner_id  = u.id
        GROUP BY u.id
        ORDER BY u.id DESC
    """).fetchall()

    # ── All leagues ───────────────────────────────────
    leagues = conn.execute("""
        SELECT l.id, l.name, l.created_at, l.invite_code,
               u.username AS commissioner,
               COUNT(DISTINCT lm.user_id) AS member_count,
               COUNT(DISTINCT t.id)       AS team_count,
               COUNT(DISTINCT p.id)       AS player_count,
               ds.is_complete             AS draft_complete
        FROM leagues l
        JOIN users u           ON l.commissioner_id = u.id
        LEFT JOIN league_members lm ON lm.league_id = l.id
        LEFT JOIN teams t           ON t.league_id  = l.id
        LEFT JOIN players p         ON p.league_id  = l.id
        LEFT JOIN draft_state ds    ON ds.league_id = l.id
        GROUP BY l.id
        ORDER BY l.created_at DESC
    """).fetchall()

    # ── Recent signups (last 10) ──────────────────────
    recent_users = conn.execute("""
        SELECT id, username, is_superadmin FROM users ORDER BY id DESC LIMIT 10
    """).fetchall()

    # ── Recent audit entries (last 20, site-wide) ─────
    recent_audit = conn.execute("""
        SELECT al.ts, al.actor, al.action, al.team, al.player,
               al.details, l.name AS league_name
        FROM audit_log al
        LEFT JOIN leagues l ON al.league_id = l.id
        ORDER BY al.id DESC LIMIT 20
    """).fetchall()

    conn.close()

    return templates.TemplateResponse("admin.html", {
        "request":         request,
        "user":            user,
        # stats
        "total_users":     total_users,
        "total_leagues":   total_leagues,
        "total_teams":     total_teams,
        "total_players":   total_players,
        "total_picks":     total_picks,
        "total_ponies":    total_ponies,
        "total_scores":    total_scores,
        "total_audits":    total_audits,
        "drafts_complete": drafts_complete,
        "drafts_active":   drafts_active,
        # tables
        "users":           [dict(r) for r in users],
        "leagues":         [dict(r) for r in leagues],
        "recent_users":    [dict(r) for r in recent_users],
        "recent_audit":    [dict(r) for r in recent_audit],
        "msg":             request.query_params.get("msg", ""),
        "error":           request.query_params.get("error", ""),
    })


@app.post("/admin/user/delete")
def admin_delete_user(user_id: int = Form(...), user=Depends(get_current_user)):
    """Delete a user account and all their owned data."""
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403)

    conn = get_db()
    target = conn.execute(adapt_sql("SELECT * FROM users WHERE id=?"), (user_id,)).fetchone()
    if not target:
        conn.close()
        return RedirectResponse("/admin?error=user_not_found", status_code=303)

    if target["is_superadmin"]:
        conn.close()
        return RedirectResponse("/admin?error=cannot_delete_superadmin", status_code=303)

    if target["id"] == user["id"]:
        conn.close()
        return RedirectResponse("/admin?error=cannot_delete_self", status_code=303)

    username = target["username"]

    # Delete leagues they commissioned (cascades to teams, players, rosters, etc.)
    owned_leagues = conn.execute(
        adapt_sql("SELECT id FROM leagues WHERE commissioner_id=?"), (user_id,)
    ).fetchall()
    for league in owned_leagues:
        conn.execute(adapt_sql("DELETE FROM audit_log WHERE league_id=?"), (league["id"],))
        conn.execute(adapt_sql("DELETE FROM leagues WHERE id=?"), (league["id"],))

    # Remove from league memberships and teams in other leagues
    conn.execute(adapt_sql("DELETE FROM league_members WHERE user_id=?"), (user_id,))
    conn.execute(adapt_sql("DELETE FROM users WHERE id=?"), (user_id,))
    conn.commit()
    conn.close()

    write_audit(actor=user["username"], action="ADMIN_USER_DELETE",
                details=f"Deleted user '{username}' (id={user_id})")
    return RedirectResponse("/admin?msg=user_deleted", status_code=303)


@app.post("/admin/league/delete")
def admin_delete_league(league_id: int = Form(...), user=Depends(get_current_user)):
    """Delete a league and all associated data."""
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403)

    conn = get_db()
    league = conn.execute(adapt_sql("SELECT * FROM leagues WHERE id=?"), (league_id,)).fetchone()
    if not league:
        conn.close()
        return RedirectResponse("/admin?error=league_not_found", status_code=303)

    league_name = league["name"]

    # Cascade deletes: roster_snapshots, team_roster, teams, players,
    # draft_state, league_members, draft_chat all have ON DELETE CASCADE.
    # audit_log has ON DELETE SET NULL so history is preserved.
    conn.execute(adapt_sql("DELETE FROM leagues WHERE id=?"), (league_id,))
    conn.commit()
    conn.close()

    write_audit(actor=user["username"], action="ADMIN_LEAGUE_DELETE",
                details=f"Deleted league '{league_name}' (id={league_id})")
    return RedirectResponse("/admin?msg=league_deleted", status_code=303)
