"""Facilitator activation capability + console-activity stamp (a0007).

Ports the legacy a010 `activate_eligible_facilitator_nominations` SQL
function verbatim (the a0002 lineage already created the tables from
the ported models). EXECUTE is granted to the web/worker caller roles
exactly as a010 did; the legacy grandfather backfill is not applicable
at genesis (existing instructor facts arrive via the V6 transfer).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0007_facilitator_activation"
down_revision = "a0006_erp_domain"
branch_labels = None
depends_on = None

_FUNCTION = r"""
CREATE FUNCTION public.activate_eligible_facilitator_nominations(
            p_tenant_id uuid,
            p_nomination_id uuid DEFAULT NULL,
            p_person_id uuid DEFAULT NULL,
            p_course_id uuid DEFAULT NULL,
            p_limit integer DEFAULT NULL,
            p_actor_person_id uuid DEFAULT NULL
        )
        RETURNS TABLE (candidate_nomination_id uuid, activated_now boolean)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_nomination record;
            v_candidate_ids uuid[];
        BEGIN
            IF p_tenant_id IS DISTINCT FROM public.app_current_tenant_id() THEN
                RAISE EXCEPTION 'facilitator activation tenant mismatch'
                    USING ERRCODE = '42501';
            END IF;
            IF p_actor_person_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM public.parties AS actor
                     JOIN public.party_persons AS actor_person ON actor_person.party_id = actor.id
                    WHERE actor.tenant_id = p_tenant_id
                      AND actor.id = p_actor_person_id
                      AND actor.party_type = 'person'
               )
            THEN
                RAISE EXCEPTION 'facilitator activation actor tenant mismatch'
                    USING ERRCODE = '22023';
            END IF;
            IF p_actor_person_id IS NOT NULL AND p_nomination_id IS NULL THEN
                RAISE EXCEPTION 'automated facilitator activation cannot name an actor'
                    USING ERRCODE = '22023';
            END IF;
            IF p_nomination_id IS NOT NULL THEN
                IF p_person_id IS NOT NULL OR p_course_id IS NOT NULL OR p_limit IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid facilitator activation selector'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF p_person_id IS NOT NULL OR p_course_id IS NOT NULL THEN
                IF p_person_id IS NULL OR p_course_id IS NULL OR p_limit IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid facilitator activation selector'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
                RAISE EXCEPTION 'invalid facilitator activation selector'
                    USING ERRCODE = '22023';
            END IF;

            IF p_limit IS NOT NULL THEN
                SELECT pg_catalog.array_agg(candidate.id ORDER BY candidate.created_at, candidate.id)
                  INTO v_candidate_ids
                  FROM (
                      SELECT n.id, n.created_at
                        FROM public.facilitator_nominations AS n
                       WHERE n.tenant_id = p_tenant_id
                         AND n.status = 'pending'
                         AND EXISTS (
                             SELECT 1
                               FROM public.parties AS person
                              JOIN public.party_persons AS subtype ON subtype.party_id = person.id
                              JOIN public.academy_account_profiles AS profile
                                ON profile.tenant_id = person.tenant_id AND profile.party_id = person.id
                              WHERE person.tenant_id = n.tenant_id
                                AND person.id = n.person_id
                                AND person.party_type = 'person'
                                AND profile.status = 'active'
                         )
                         AND EXISTS (
                             SELECT 1
                               FROM public.facilitator_requirements AS required
                              WHERE required.tenant_id = n.tenant_id
                                AND required.nomination_id = n.id
                         )
                         AND NOT EXISTS (
                             SELECT 1
                               FROM public.facilitator_requirements AS required
                              WHERE required.tenant_id = n.tenant_id
                                AND required.nomination_id = n.id
                                AND NOT EXISTS (
                                    SELECT 1
                                      FROM mod_learning.course_completions AS completed
                                     WHERE completed.tenant_id = required.tenant_id
                                       AND completed.person_id = n.person_id
                                       AND completed.course_id = required.course_id
                                       AND completed.status = 'completed'
                                )
                         )
                       ORDER BY n.created_at, n.id
                       LIMIT p_limit
                         FOR UPDATE OF n SKIP LOCKED
                  ) AS candidate;
            ELSE
                SELECT pg_catalog.array_agg(candidate.id ORDER BY candidate.created_at, candidate.id)
                  INTO v_candidate_ids
                  FROM (
                      SELECT n.id, n.created_at
                        FROM public.facilitator_nominations AS n
                       WHERE n.tenant_id = p_tenant_id
                         AND (
                             n.id = p_nomination_id
                             OR (
                                 p_person_id IS NOT NULL
                                 AND n.person_id = p_person_id
                                 AND n.status = 'pending'
                                 AND EXISTS (
                                     SELECT 1
                                       FROM public.facilitator_requirements AS affected
                                      WHERE affected.tenant_id = n.tenant_id
                                        AND affected.nomination_id = n.id
                                        AND affected.course_id = p_course_id
                                 )
                             )
                         )
                       ORDER BY n.created_at, n.id
                         FOR UPDATE OF n
                  ) AS candidate;
            END IF;

            FOR v_nomination IN
                SELECT n.*
                  FROM public.facilitator_nominations AS n
                 WHERE n.id = ANY(COALESCE(v_candidate_ids, ARRAY[]::uuid[]))
                 ORDER BY n.created_at, n.id
            LOOP
                candidate_nomination_id := v_nomination.id;
                activated_now := false;

                IF p_actor_person_id IS NOT NULL
                   AND v_nomination.nominated_by_person_id IS DISTINCT FROM p_actor_person_id
                THEN
                    RAISE EXCEPTION 'facilitator activation actor does not own nomination provenance'
                        USING ERRCODE = '22023';
                END IF;

                IF v_nomination.status = 'pending'
                   AND EXISTS (
                       SELECT 1
                         FROM public.parties AS candidate
                        JOIN public.party_persons AS subtype ON subtype.party_id = candidate.id
                        JOIN public.academy_account_profiles AS profile
                          ON profile.tenant_id = candidate.tenant_id AND profile.party_id = candidate.id
                        WHERE candidate.tenant_id = v_nomination.tenant_id
                          AND candidate.id = v_nomination.person_id
                          AND candidate.party_type = 'person'
                          AND profile.status = 'active'
                   )
                   AND EXISTS (
                       SELECT 1
                         FROM public.facilitator_requirements AS required
                        WHERE required.tenant_id = v_nomination.tenant_id
                          AND required.nomination_id = v_nomination.id
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.facilitator_requirements AS required
                        WHERE required.tenant_id = v_nomination.tenant_id
                          AND required.nomination_id = v_nomination.id
                          AND NOT EXISTS (
                              SELECT 1
                                FROM mod_learning.course_completions AS completed
                               WHERE completed.tenant_id = required.tenant_id
                                 AND completed.person_id = v_nomination.person_id
                                 AND completed.course_id = required.course_id
                                 AND completed.status = 'completed'
                          )
                   )
                THEN
                    UPDATE public.facilitator_nominations
                       SET status = 'active',
                           activated_at = pg_catalog.clock_timestamp(),
                           revoked_at = NULL,
                           activation_reason = 'requirements_completed',
                           updated_at = pg_catalog.clock_timestamp()
                     WHERE tenant_id = v_nomination.tenant_id
                       AND id = v_nomination.id
                       AND status = 'pending';

                    IF FOUND THEN
                        activated_now := true;
                        INSERT INTO public.notifications (
                            id, tenant_id, person_id, kind, title, body, link
                        ) VALUES (
                            pg_catalog.gen_random_uuid(),
                            v_nomination.tenant_id,
                            v_nomination.person_id,
                            'facilitator_activated',
                            'Your facilitator assignment is active',
                            'You completed the required course work and can now facilitate '
                            'the assigned learning scope.',
                            '/learn'
                        );
                        INSERT INTO public.audit_events (
                            id, tenant_id, actor_type, actor_id, actor_party_id, source_application, action,
                            entity_type, entity_id, details
                        ) VALUES (
                            pg_catalog.gen_random_uuid(),
                            v_nomination.tenant_id,
                            CASE WHEN p_actor_person_id IS NULL THEN 'system' ELSE 'user' END,
                            CASE WHEN p_actor_person_id IS NULL THEN 'facilitator_activation'
                                 ELSE p_actor_person_id::text END,
                            p_actor_person_id,
                            'dotmac_academy_app',
                            'facilitator_nomination.activate',
                            'facilitator_nomination',
                            v_nomination.id::text,
                            pg_catalog.jsonb_build_object(
                                'scope', v_nomination.scope_type,
                                'person_id', v_nomination.person_id::text
                            )
                        );
                    END IF;
                END IF;
                RETURN NEXT;
            END LOOP;
        END;
        $$;
"""

_SIGNATURE = "public.activate_eligible_facilitator_nominations(uuid, uuid, uuid, uuid, integer, uuid)"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT current_user")) != "app_admin":
        raise ValueError("Academy assembly migration requires app_admin")
    op.execute(_FUNCTION)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO app_admin")
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO app_user, platform_api, academy_lab_worker")
    # Legacy a008: the web role may write last_seen_at, but the database
    # clock always wins (a client-supplied future stamp must not keep a
    # lab alive forever).
    op.execute(
        "CREATE FUNCTION public.lab_console_activity_stamp() RETURNS trigger "
        "LANGUAGE plpgsql SET search_path = pg_catalog AS $$BEGIN NEW.last_seen_at := pg_catalog.now(); RETURN NEW; END$$"
    )
    op.execute("ALTER FUNCTION public.lab_console_activity_stamp() OWNER TO app_admin")
    op.execute("REVOKE ALL ON FUNCTION public.lab_console_activity_stamp() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER lab_console_activity_stamp BEFORE INSERT OR UPDATE "
        "ON public.lab_console_activity FOR EACH ROW "
        "EXECUTE FUNCTION public.lab_console_activity_stamp()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS lab_console_activity_stamp ON public.lab_console_activity")
    op.execute("DROP FUNCTION IF EXISTS public.lab_console_activity_stamp()")
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
