from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.campaigns import (
    CampaignValidationError,
    add_campaign_membership,
    archive_campaign,
    build_campaign_plan,
    create_campaign,
    create_campaign_execution_plan,
    create_saved_search,
    update_saved_search,
)
from app.models import (
    Base,
    Campaign,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignSavedSearch,
)
from tests.test_main import _request


@dataclass
class FakeFormRequest:
    payload: dict[str, str]

    async def form(self) -> dict[str, str]:
        return self.payload

    @property
    def query_params(self) -> dict[str, str]:
        return {}


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _saved_search(
    db,
    name: str = "Strategy Sydney",
    query: str = '"strategy analyst" OR "commercial analyst"',
):
    return create_saved_search(
        db,
        name=name,
        source="seek",
        query_text=query,
        location="Sydney NSW",
        date_window="last_7_days",
        max_pages=2,
    )


def test_saved_search_validation_preserves_exact_boolean_query() -> None:
    session_factory = _session_factory()
    exact_query = '"strategy analyst" OR ("commercial analyst" AND graduate)'

    with session_factory() as db:
        saved = _saved_search(db, query=exact_query)

        assert saved.query_text == exact_query


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "name is required"),
        ("query_text", "", "query is required"),
        ("source", "prosple", "not supported"),
        ("date_window", "today", "Unsupported date window"),
        ("max_pages", "99", "Maximum pages"),
    ],
)
def test_saved_search_validation_rejects_invalid_values(field, value, message) -> None:
    session_factory = _session_factory()
    payload = {
        "name": "Strategy Sydney",
        "source": "seek",
        "query_text": "strategy",
        "location": "Sydney NSW",
        "date_window": "last_7_days",
        "max_pages": 2,
    }
    payload[field] = int(value) if field == "max_pages" else value

    with session_factory() as db, pytest.raises(CampaignValidationError, match=message):
        create_saved_search(db, **payload)


def test_campaign_plan_rejects_empty_archived_and_duplicate_membership() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Graduate campaign")
        empty_plan = build_campaign_plan(db, campaign)
        assert "no runnable saved searches" in " ".join(empty_plan.errors)

        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved, position=1)
        with pytest.raises(CampaignValidationError, match="already"):
            add_campaign_membership(db, campaign, saved, position=2)

        archive_campaign(db, campaign)
        archived_plan = build_campaign_plan(db, campaign)
        assert "Archived campaigns" in " ".join(archived_plan.errors)


def test_campaign_plan_ordering_disabled_omission_and_planned_pages() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Weekly reconciliation")
        first = _saved_search(db, "First")
        second = _saved_search(db, "Second")
        third = _saved_search(db, "Disabled")
        add_campaign_membership(db, campaign, second, position=2)
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, third, position=3, is_enabled=False)

        plan = build_campaign_plan(db, campaign)

        assert [item.saved_search.name for item in plan.items] == ["First", "Second"]
        assert plan.planned_child_count == 2
        assert plan.planned_pages == 4


def test_campaign_plan_reports_unsupported_enabled_source() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Unsupported source")
        saved = _saved_search(db)
        saved.source = "linkedin"
        db.commit()
        add_campaign_membership(db, campaign, saved)

        plan = build_campaign_plan(db, campaign)

        assert "not supported" in " ".join(plan.errors)
        assert plan.items == []


def test_snapshot_creation_is_immutable_after_saved_search_edit() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Daily overlap")
        saved = _saved_search(db, query='"strategy analyst" OR "growth analyst"')
        add_campaign_membership(db, campaign, saved, position=1)

        execution = create_campaign_execution_plan(db, campaign)
        update_saved_search(
            db,
            saved,
            name="Renamed",
            source="seek",
            query_text="changed query",
            location="Melbourne VIC",
            date_window="last_3_days",
            max_pages=1,
            is_enabled=True,
            is_archived=False,
        )
        snapshot = db.scalar(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution.id
            )
        )

        assert execution.status.value == "pending"
        assert execution.planned_child_count == 1
        assert execution.pages_planned == 2
        assert snapshot.saved_search_name_snapshot == "Strategy Sydney"
        assert snapshot.query_text_snapshot == '"strategy analyst" OR "growth analyst"'
        assert snapshot.location_snapshot == "Sydney NSW"
        assert snapshot.date_window_snapshot == "last_7_days"
        assert snapshot.page_limit_snapshot == 2


def test_migration_creates_campaign_tables_idempotently() -> None:
    from app.migrations import migrate_database

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    migrate_database(engine)
    migrate_database(engine)

    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        campaign = create_campaign(db, name="Migration smoke")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)

        assert db.scalar(select(Campaign).where(Campaign.id == campaign.id)) is not None
        assert db.scalar(select(CampaignSavedSearch)) is not None
        assert db.scalar(select(CampaignExecution).where(CampaignExecution.id == execution.id))


@pytest.mark.asyncio
async def test_campaign_create_edit_archive_and_detail_routes() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        response = await main.create_campaign_route(
            FakeFormRequest({"name": "Route campaign", "description": "Daily checks"}),
            db=db,
        )
        assert response.status_code == 303
        campaign = db.scalar(select(Campaign).where(Campaign.name == "Route campaign"))
        saved = _saved_search(db, "Route search")
        add_campaign_membership(db, campaign, saved)

        detail = main.campaign_detail(campaign.id, _request(f"/campaigns/{campaign.id}"), db=db)
        assert "Route campaign" in detail.body.decode()
        assert "mobile-card-list" in detail.body.decode()

        response = await main.update_campaign_route(
            campaign.id,
            FakeFormRequest(
                {"name": "Route campaign edited", "description": "Updated", "is_active": "yes"}
            ),
            db=db,
        )
        assert response.status_code == 303

        archive = main.archive_campaign_route(campaign.id, db=db)
        assert archive.status_code == 303
        assert db.get(Campaign, campaign.id).is_archived is True


@pytest.mark.asyncio
async def test_saved_search_add_preview_and_execution_routes_do_not_collect(monkeypatch) -> None:
    session_factory = _session_factory()
    calls = []
    monkeypatch.setattr(main, "get_collector", lambda *args, **kwargs: calls.append("collector"))

    with session_factory() as db:
        campaign = create_campaign(db, name="Preview campaign")
        response = await main.create_saved_search_for_campaign(
            campaign.id,
            FakeFormRequest(
                {
                    "name": "Boolean search",
                    "source": "seek",
                    "query_text": '"strategy analyst" OR "commercial analyst"',
                    "location": "Sydney NSW",
                    "date_window": "last_2_days",
                    "max_pages": "2",
                    "is_enabled": "yes",
                }
            ),
            db=db,
        )
        assert response.status_code == 303

        preview = main.campaign_preview(
            campaign.id, _request(f"/campaigns/{campaign.id}/preview"), db=db
        )
        body = preview.body.decode()
        assert "Execution preview" in body
        assert "Last 2 days" in body
        assert "starts no browser" in body

        created = main.create_campaign_execution_route(campaign.id, db=db)
        assert created.status_code == 303
        execution = db.scalar(select(CampaignExecution))
        detail = main.campaign_execution_detail(
            execution.id, _request(f"/campaign-executions/{execution.id}"), db=db
        )
        assert "Stored execution snapshot" in detail.body.decode()
        assert "&#34;strategy analyst&#34; OR &#34;commercial analyst&#34;" in detail.body.decode()
        assert calls == []


def test_campaign_dashboard_foundation() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Dashboard campaign")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        create_campaign_execution_plan(db, campaign)

        dashboard = main.index(_request("/"), db=db)
        body = dashboard.body.decode()
        assert "Active campaigns" in body
        assert "Saved searches" in body
        assert "Latest campaign plan" in body


def test_campaign_detail_missing_uses_404() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            main.campaign_detail(999, _request("/campaigns/999"), db=db)
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_custom_404_renders_safely_for_campaign_area() -> None:
    response = await main.not_found_page(_request("/campaigns/missing"), HTTPException(404))

    assert response.status_code == 404
    assert "could not be found" in response.body.decode()
