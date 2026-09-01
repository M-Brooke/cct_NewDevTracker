"""
NDA Site & Land Use capture app.

Skeleton / starting point, not production-ready. Two capture modes:
  - Edit existing site: pick a site already in the Sites layer, edit attributes
    and its related Land_Uses rows.
  - Draw new site: occasional path for a genuinely new, freestanding boundary.

Writes directly to an ArcGIS Online / Enterprise hosted feature layer
(Sites polygons) + related table (Land_Uses), replacing the Excel + GDB
reconciliation step in the existing notebook.

TODO before this runs:
  - Fill in PORTAL_URL / CLIENT_ID for OAuth2.
  - Fill in SITES_LAYER_URL / LAND_USES_TABLE_URL.
  - Confirm the foreign-key field name Land_Uses uses to relate to Sites
    (SITE_FK_FIELD below) - check the relationship class definition.
  - Replace ZONING_LAND_USE_MAP with the real var_lu lookup (load from a
    CSV or the choices tab we built for the XLSForm version).
"""

import streamlit as st
import pandas as pd
import hashlib
import json
from shapely.geometry import shape as shapely_shape
from arcgis.gis import GIS
from arcgis.features import FeatureLayer, Table
from arcgis.geometry import Geometry
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

# ---------------------------------------------------------------------------
# Config - TODO: fill these in for your Portal / feature service
# ---------------------------------------------------------------------------
PORTAL_URL = "https://your-org.maps.arcgis.com"
CLIENT_ID = "YOUR_OAUTH_CLIENT_ID"

SITES_LAYER_URL = "https://services.arcgis.com/xxxx/arcgis/rest/services/NDA_Sites/FeatureServer/0"
LAND_USES_TABLE_URL = "https://services.arcgis.com/xxxx/arcgis/rest/services/NDA_Sites/FeatureServer/1"

SITE_ID_FIELD = "Site_ID"
SITE_FK_FIELD = "Site_GlobalID"  # foreign key on Land_Uses pointing back to Sites.GlobalID

# TODO: replace with the real var_lu-derived lookup - {zoning_code: [permitted land uses]}
ZONING_LAND_USE_MAP = {
    "GR2": ["RES_Single", "RES_Intermediate", "RES_Multi"],
    "GR3": ["RES_Intermediate", "RES_Multi"],
    "GB3": ["NRES_Office", "NRES_Hospitality", "NRES_Retail"],
    "MU2": ["RES_Multi", "NRES_Office"],
}

# Choice lists pulled from the actual values in data_in - confirm these are
# complete against the full historical dataset before publishing.
REVIEW_CATEGORY_OPTIONS = ["Addition", "Amendment", "Removal"]
UPDATE_REASON_OPTIONS = [
    "Buildings in Construction or Completed",
    "Change in Extent of land parcel due to DIM or SPC (specify in Notes column)",
    "Decrease in yield due to DIM or SPC",
    "Increase in yield due to SPC",
    "Migrate to MSA (Managed Settlement Area)",
    "Other (specify in Notes column)",
    "Public Input (significant)",
]
DEV_TYPE_OPTIONS = [
    "Addition\\Bulking-up\\Densification",
    "Alteration\\Redevelopment\\Partial Substitution",
    "Demolition\\Full Substitution",
    "Informal RES",
    "New (vacant\\infill)",
]
PARKING_ZONE_OPTIONS = [
    "PZ0 (No requirement)",
    "PZ1 (same as PT1)",
    "PZ2 (same as PT2)",
    "PZ3 (Standard Parking Zone)",
]

# Site-level free-text fields Update_Notes and Review_Notes, plus LIS_IDs
# and Development_Location_Issue, are wired up directly in the form below.

EMPTY_LAND_USE_ROW = {
    "Land_Use_Dscr": None,
    "Land_Use_Perc": 0.0,
    "RES_Avg_Unit_Size": None,
    "RES_Avg_Value_ZAR": None,
    "Proposed_RES_Erf_Sz": None,
    "Parking_Zone": None,
    "DUs_Fix_Actual": None,
    "GLA_Fix_Actual": None,
}


# ---------------------------------------------------------------------------
# Auth + layer connections
# ---------------------------------------------------------------------------
def get_gis() -> GIS:
    """Authenticate once per browser session, not once per server process.

    @st.cache_resource would cache this across ALL users - the first person
    to log in would end up being the identity every subsequent user's edits
    get attributed to. Session state keeps it scoped to one browser tab.
    """
    if "gis" not in st.session_state:
        st.session_state.gis = GIS(PORTAL_URL, client_id=CLIENT_ID)
    return st.session_state.gis


def get_layers(_gis: GIS):
    # FeatureLayer/Table objects are cheap to construct and just wrap the
    # URL + gis reference - fine to rebuild each run, no caching needed.
    sites = FeatureLayer(SITES_LAYER_URL, gis=_gis)
    land_uses = Table(LAND_USES_TABLE_URL, gis=_gis)
    return sites, land_uses


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def search_sites(sites_layer: FeatureLayer, query_text: str) -> pd.DataFrame:
    if not query_text:
        return pd.DataFrame()
    where = f"{SITE_ID_FIELD} LIKE '%{query_text}%'"
    fset = sites_layer.query(where=where, out_fields="*", return_geometry=False)
    return fset.sdf


def load_site_and_land_uses(sites_layer, land_uses_table, site_global_id: str):
    """Load a site's attributes + geometry, and its related Land_Uses rows.

    Also returns EditDate (if the layer has editor tracking enabled) so the
    save step can detect if someone else edited this site in the meantime -
    see check_not_stale() below.
    """
    site_fset = sites_layer.query(where=f"GlobalID = '{site_global_id}'", out_fields="*")
    site_feature = site_fset.features[0]
    site_row = site_feature.attributes
    site_geometry = site_feature.geometry  # None if the layer has no geometry or query failed

    lu_cols = list(EMPTY_LAND_USE_ROW.keys())
    lu_fset = land_uses_table.query(where=f"{SITE_FK_FIELD} = '{site_global_id}'", out_fields="*")
    lu_df = lu_fset.sdf[lu_cols] if len(lu_fset.sdf) else pd.DataFrame([EMPTY_LAND_USE_ROW])

    return site_row, lu_df, site_geometry


def check_not_stale(sites_layer, site_global_id: str, loaded_edit_date) -> bool:
    """Re-query EditDate right before writing. Returns True if safe to save,
    False if someone else has edited this site since it was loaded.

    Requires editor tracking to be enabled on the Sites layer (EditDate
    field present) - if it isn't, this silently allows the save, since
    there's nothing to compare against."""
    if loaded_edit_date is None:
        return True
    current = sites_layer.query(
        where=f"GlobalID = '{site_global_id}'", out_fields="EditDate"
    ).sdf
    if current.empty:
        return True
    return current.iloc[0]["EditDate"] == loaded_edit_date


def geojson_draw_to_esri_geometry(geojson_geom: dict, target_spatial_reference: dict) -> Geometry:
    """Convert a folium Draw GeoJSON polygon to an Esri geometry in the
    TARGET layer's spatial reference.

    folium/Leaflet always draws in WGS84 (EPSG:4326) regardless of the
    service's actual spatial reference, so this explicitly projects rather
    than just relabelling the coordinates - skipping that step silently
    places the geometry in the wrong location unless the service happens
    to already be WGS84.
    """
    shp = shapely_shape(geojson_geom)
    wgs84_geom = Geometry.from_shapely(shp, spatial_reference={"wkid": 4326})
    if target_spatial_reference.get("wkid") in (4326, None):
        return wgs84_geom
    from arcgis.geometry import project
    projected = project([wgs84_geom], in_sr=4326, out_sr=target_spatial_reference)
    return projected[0]


def diff_site_attrs(old: dict, new: dict) -> list[tuple[str, object, object]]:
    """Field-by-field diff for the review-before-save step."""
    changes = []
    for field, new_val in new.items():
        old_val = old.get(field)
        # Loose comparison - avoids false positives from e.g. 0 vs 0.0 or None vs ""
        if str(old_val or "") != str(new_val or ""):
            changes.append((field, old_val, new_val))
    return changes


def diff_land_uses(old_df: pd.DataFrame, new_df: pd.DataFrame) -> list[str]:
    """Coarse row-level diff - good enough for a review step, not a full
    reconciliation (that happens in _replace_land_use_rows regardless)."""
    old_rows = old_df.dropna(subset=["Land_Use_Dscr"])
    new_rows = new_df.dropna(subset=["Land_Use_Dscr"])
    old_set = {tuple(r) for r in old_rows.astype(str).values.tolist()}
    new_set = {tuple(r) for r in new_rows.astype(str).values.tolist()}

    messages = []
    if len(old_rows) != len(new_rows):
        messages.append(f"Land use row count: {len(old_rows)} -> {len(new_rows)}")
    if old_set != new_set:
        messages.append("Land use row contents changed.")
    return messages


def validate_land_uses(land_use_df: pd.DataFrame, zoning_code: str, review_category: str = "") -> list[str]:
    """Return a list of human-readable validation errors, empty if clean.

    Mirrors the source workbook's Control_Site_Perc / Control_Land_Use logic:
    it flags the total exceeding 100%, not failing to reach exactly 100% -
    a site can legitimately have some undetermined/unallocated extent.

    Also mirrors "Removal implies no yield": a site being marked Removal
    isn't required to have land uses at all, and if it still has nonzero
    Land_Use_Perc left over, that's flagged as something to clean up rather
    than a hard error - same soft-touch as the source workbook.
    """
    errors = []
    rows = land_use_df.dropna(subset=["Land_Use_Dscr"])

    if review_category == "Removal":
        leftover = rows["Land_Use_Perc"].sum() if not rows.empty else 0
        if leftover > 0:
            errors.append(
                f"Review_Category is 'Removal' but land uses still total {leftover:.2f} - "
                "consider clearing land use rows so this site doesn't contribute yield."
            )
        return errors

    if rows.empty:
        errors.append("Add at least one land use.")
        return errors

    permitted = ZONING_LAND_USE_MAP.get(zoning_code, [])
    invalid = set(rows["Land_Use_Dscr"]) - set(permitted)
    if invalid:
        errors.append(f"Not permitted under {zoning_code}: {', '.join(sorted(invalid))}")

    total_pct = rows["Land_Use_Perc"].sum()
    if total_pct > 1.001:
        errors.append(f"Land_Use_Perc totals {total_pct:.2f} - site development exceeds 100%")

    return errors


def validate_timeframes(pct_2030: float, pct_2050: float, pct_beyond_2050: float) -> list[str]:
    """Mirrors Control_Time_Perc: flags a phasing error only if the three
    timeframe percentages sum to more than 100%."""
    total = (pct_2030 or 0) + (pct_2050 or 0) + (pct_beyond_2050 or 0)
    if total > 1.001:
        return [f"Timeframe percentages total {total:.2f} - phasing error, must not exceed 1.0"]
    return []


class PartialSaveError(RuntimeError):
    """Raised when some but not all of a save succeeded.

    Carries the site's GlobalID (if a new site row was created) so the
    caller can stash it in session state and retry as an update rather
    than creating a duplicate site.
    """

    def __init__(self, message: str, site_global_id: str | None):
        super().__init__(message)
        self.site_global_id = site_global_id


def _replace_land_use_rows(land_uses_table, site_global_id: str, land_use_df: pd.DataFrame):
    """Delete this site's existing Land_Uses rows and add the new ones in a
    single applyEdits call. rollback_on_failure=True (the REST API default)
    means the whole call succeeds or fails together - there is never a
    moment where the site has zero land-use rows on the server."""
    old = land_uses_table.query(
        where=f"{SITE_FK_FIELD} = '{site_global_id}'", out_fields="OBJECTID"
    ).sdf
    deletes = old["OBJECTID"].tolist() if len(old) else []

    land_use_rows = land_use_df.dropna(subset=["Land_Use_Dscr"])
    adds = [
        {"attributes": {SITE_FK_FIELD: site_global_id, **row.to_dict()}}
        for _, row in land_use_rows.iterrows()
    ]

    result = land_uses_table.edit_features(deletes=deletes, adds=adds, rollback_on_failure=True)

    failed = [r for r in result.get("addResults", []) + result.get("deleteResults", []) if not r["success"]]
    if failed:
        # rollback_on_failure means the server already rolled this back -
        # the old rows are intact. Safe to just tell the user to retry.
        raise RuntimeError(f"Land use save failed and was rolled back: {failed}")

    return result


def write_site_and_land_uses(
    sites_layer, land_uses_table, site_attrs: dict, geometry: Geometry | None,
    land_use_df: pd.DataFrame, existing_global_id: str | None,
):
    """Add a new site or update an existing one, and replace its Land_Uses rows.

    Ordering is deliberate:
      - Existing site: land uses are replaced FIRST, site attributes SECOND.
        If the land-use write fails, nothing has changed yet - just retry.
      - New site: the site row must be added first (its GlobalID is the
        foreign key the land-use rows need). If the land-use write then
        fails, we raise PartialSaveError carrying that GlobalID so the
        caller can switch to update-mode on retry instead of creating a
        second site.
    """
    feature_dict = {"attributes": dict(site_attrs)}
    if geometry is not None:
        feature_dict["geometry"] = geometry

    if existing_global_id:
        # Land uses first - if this fails, the site attribute update below
        # never runs, so nothing is left half-changed.
        lu_result = _replace_land_use_rows(land_uses_table, existing_global_id, land_use_df)

        feature_dict["attributes"]["GlobalID"] = existing_global_id
        site_result = sites_layer.edit_features(updates=[feature_dict])
        update_result = site_result["updateResults"][0]
        if not update_result["success"]:
            # Land uses already saved successfully - tell the user plainly
            # rather than silently reporting overall success.
            raise PartialSaveError(
                f"Land uses saved, but site attribute update failed: {update_result}. "
                "Land use data is safe - just retry saving the site attributes.",
                existing_global_id,
            )
        return site_result, lu_result

    else:
        site_result = sites_layer.edit_features(adds=[feature_dict])
        add_result = site_result["addResults"][0]
        if not add_result["success"]:
            raise RuntimeError(f"Site add failed: {add_result}")

        new_oid = add_result["objectId"]
        site_global_id = sites_layer.query(
            object_ids=str(new_oid), out_fields="GlobalID"
        ).sdf.iloc[0]["GlobalID"]

        try:
            lu_result = _replace_land_use_rows(land_uses_table, site_global_id, land_use_df)
        except RuntimeError as exc:
            # Site row now exists with no land uses yet. Surface the
            # GlobalID so the caller can store it and retry as an update,
            # instead of creating a duplicate site on the next attempt.
            raise PartialSaveError(
                f"Site was created (ID: {site_attrs.get(SITE_ID_FIELD)}) but land uses "
                f"failed to save: {exc}. Click Save again to retry adding land uses "
                "to this site.",
                site_global_id,
            ) from exc

        return site_result, lu_result


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NDA Capture", layout="wide")
st.title("NDA site & land use capture")

gis = get_gis()
sites_layer, land_uses_table = get_layers(gis)

if "land_use_df" not in st.session_state:
    st.session_state.land_use_df = pd.DataFrame([EMPTY_LAND_USE_ROW])
if "drawn_geometry" not in st.session_state:
    st.session_state.drawn_geometry = None
if "existing_global_id" not in st.session_state:
    st.session_state.existing_global_id = None
if "loaded_geometry" not in st.session_state:
    st.session_state.loaded_geometry = None
if "loaded_edit_date" not in st.session_state:
    st.session_state.loaded_edit_date = None
if "adjust_boundary" not in st.session_state:
    st.session_state.adjust_boundary = False
if "reviewed_hash" not in st.session_state:
    st.session_state.reviewed_hash = None
if "reviewed_diff" not in st.session_state:
    st.session_state.reviewed_diff = None

mode = st.radio("Mode", ["Edit existing site", "Draw new site"], horizontal=True)

if st.session_state.get("last_mode") != mode:
    # Switching modes - clear out state left over from whatever was being
    # edited before, so a stale site's land uses don't leak into a new one.
    st.session_state.land_use_df = pd.DataFrame([EMPTY_LAND_USE_ROW])
    st.session_state.drawn_geometry = None
    st.session_state.existing_global_id = None
    st.session_state.loaded_geometry = None
    st.session_state.loaded_edit_date = None
    st.session_state.adjust_boundary = False
    st.session_state.reviewed_hash = None
    st.session_state.last_mode = mode

site_attrs = {}

if mode == "Edit existing site":
    search = st.text_input("Search Site_ID")
    matches = search_sites(sites_layer, search)
    if len(matches):
        choice = st.selectbox("Select site", matches[SITE_ID_FIELD].tolist())
        chosen = matches[matches[SITE_ID_FIELD] == choice].iloc[0]

        # Only reload from the server when the selection actually changes -
        # otherwise every rerun (e.g. typing in a field below) would wipe
        # out in-progress edits.
        if st.session_state.existing_global_id != chosen["GlobalID"]:
            site_row, lu_df, geometry = load_site_and_land_uses(
                sites_layer, land_uses_table, chosen["GlobalID"]
            )
            st.session_state.existing_global_id = chosen["GlobalID"]
            st.session_state.land_use_df = lu_df
            st.session_state.loaded_geometry = geometry
            st.session_state.loaded_edit_date = site_row.get("EditDate")
            st.session_state.adjust_boundary = False
            st.session_state.reviewed_hash = None
            st.session_state._site_row_cache = site_row

        site_attrs.update(st.session_state._site_row_cache)

        st.session_state.adjust_boundary = st.checkbox(
            "Adjust this site's boundary", value=st.session_state.adjust_boundary
        )
        m = folium.Map(tiles="Esri.WorldImagery", zoom_start=15)
        if st.session_state.loaded_geometry is not None:
            shp = st.session_state.loaded_geometry.as_shapely
            gj = folium.GeoJson(
                shp.__geo_interface__,
                style_function=lambda _: {"color": "#3388ff", "weight": 2, "fillOpacity": 0.1},
                name="current boundary",
            )
            gj.add_to(m)
            b = shp.bounds  # (minx, miny, maxx, maxy)
            m.fit_bounds([[b[1], b[0]], [b[3], b[2]]])
        if st.session_state.adjust_boundary:
            st.caption("Existing boundary shown in blue for reference - draw its replacement.")
            Draw(export=False, draw_options={"polygon": True, "polyline": False, "rectangle": False,
                                              "circle": False, "marker": False, "circlemarker": False}
                 ).add_to(m)
            map_output = st_folium(m, height=400, use_container_width=True, key="edit_map")
            if map_output and map_output.get("last_active_drawing"):
                st.session_state.drawn_geometry = map_output["last_active_drawing"]["geometry"]
                st.success("Replacement boundary captured.")
        else:
            st_folium(m, height=300, use_container_width=True, key="ref_map")
            st.session_state.drawn_geometry = None
    else:
        st.session_state.existing_global_id = None
        st.info("Search for a Site_ID to load it.")

else:
    st.write("Draw the new site boundary on the map, then fill in attributes below.")
    m = folium.Map(tiles="Esri.WorldImagery", location=[-33.9, 18.6], zoom_start=13)
    Draw(export=False, draw_options={"polygon": True, "polyline": False, "rectangle": False,
                                      "circle": False, "marker": False, "circlemarker": False}
         ).add_to(m)
    map_output = st_folium(m, height=450, use_container_width=True, key="new_map")

    if map_output and map_output.get("last_active_drawing"):
        st.session_state.drawn_geometry = map_output["last_active_drawing"]["geometry"]
        st.success("Boundary captured.")
    st.session_state.existing_global_id = None
    st.session_state.loaded_geometry = None
    st.session_state.loaded_edit_date = None

st.divider()
st.subheader("Site details")

col1, col2, col3 = st.columns(3)
with col1:
    site_id = st.text_input("Site_ID", value=site_attrs.get(SITE_ID_FIELD, ""))
    district_code = st.text_input("District_Code", value=site_attrs.get("District_Code", ""))
    site_extent_sqm = st.number_input(
        "Site_Extent_SQM", value=float(site_attrs.get("Site_Extent_SQM", 0.0)), min_value=0.0
    )
with col2:
    zoning_code = st.selectbox(
        "Zoning_Code", options=list(ZONING_LAND_USE_MAP.keys()),
        index=list(ZONING_LAND_USE_MAP.keys()).index(site_attrs["Zoning_Code"])
        if site_attrs.get("Zoning_Code") in ZONING_LAND_USE_MAP else 0,
    )
    dev_type = st.selectbox(
        "Dev_Type", options=DEV_TYPE_OPTIONS,
        index=DEV_TYPE_OPTIONS.index(site_attrs["Dev_Type"])
        if site_attrs.get("Dev_Type") in DEV_TYPE_OPTIONS else 0,
    )
    sdp_available = st.checkbox("SDP_Available", value=bool(site_attrs.get("SDP_Available", False)))
with col3:
    review_category = st.selectbox(
        "Review_Category", options=REVIEW_CATEGORY_OPTIONS,
        index=REVIEW_CATEGORY_OPTIONS.index(site_attrs["Review_Category"])
        if site_attrs.get("Review_Category") in REVIEW_CATEGORY_OPTIONS else 0,
    )
    update_reason = st.selectbox(
        "Update_Reason", options=UPDATE_REASON_OPTIONS,
        index=UPDATE_REASON_OPTIONS.index(site_attrs["Update_Reason"])
        if site_attrs.get("Update_Reason") in UPDATE_REASON_OPTIONS else 0,
    )

lis_ids = st.text_input("LIS_IDs", value=site_attrs.get("LIS_IDs", ""),
                         help="Semicolon-separated if multiple parcels - for info only.")
development_location_issue = st.text_input(
    "Development_Location_Issue", value=site_attrs.get("Development_Location_Issue", "")
)
update_notes = st.text_area("Update_Notes", value=site_attrs.get("Update_Notes", ""))
review_notes = st.text_area("Review_Notes", value=site_attrs.get("Review_Notes", ""))

with st.expander("Timeframe split & certainty"):
    tcol1, tcol2, tcol3 = st.columns(3)
    with tcol1:
        pct_2030 = st.number_input(
            "Perc_Complete_around_2030", value=float(site_attrs.get("Perc_Complete_around_2030", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )
        certainty_use = st.number_input(
            "Certainty_Use", value=float(site_attrs.get("Certainty_Use", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )
    with tcol2:
        pct_2050 = st.number_input(
            "Perc_Complete_by_2050", value=float(site_attrs.get("Perc_Complete_by_2050", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )
        certainty_intensity = st.number_input(
            "Certainty_Intensity", value=float(site_attrs.get("Certainty_Intensity", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )
    with tcol3:
        pct_beyond_2050 = st.number_input(
            "Perc_Complete_beyond_2050", value=float(site_attrs.get("Perc_Complete_beyond_2050", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )
        certainty_timeframe = st.number_input(
            "Certainty_TimeFrame", value=float(site_attrs.get("Certainty_TimeFrame", 0.0)),
            min_value=0.0, max_value=1.0, step=0.05,
        )

if mode == "Edit existing site" and site_attrs:
    with st.expander("Previous update history (read-only)"):
        st.caption(
            f"Category: {site_attrs.get('Previous_Update_Category', '-')}  |  "
            f"Reason: {site_attrs.get('Previous_Update_Reason', '-')}"
        )
        st.caption(f"Notes: {site_attrs.get('Previous_Update_Notes', '-')}")

st.subheader("Land uses")
st.caption(
    "Permitted uses under the selected zoning: " + ", ".join(ZONING_LAND_USE_MAP.get(zoning_code, []))
)
# Note: st.data_editor's SelectboxColumn options apply to the whole column,
# not per-row, so it can't hard-filter choices by zoning the way an
# XLSForm choice_filter can. Options are left open here and checked in
# validate_land_uses() instead - tighten later with a custom widget if needed.
edited_land_uses = st.data_editor(
    st.session_state.land_use_df,
    num_rows="dynamic",
    column_config={
        "Land_Use_Dscr": st.column_config.SelectboxColumn(
            options=sorted({u for uses in ZONING_LAND_USE_MAP.values() for u in uses})
        ),
        "Land_Use_Perc": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.01),
        "RES_Avg_Unit_Size": st.column_config.NumberColumn(min_value=0.0, help="RES land uses only"),
        "RES_Avg_Value_ZAR": st.column_config.NumberColumn(min_value=0.0, help="RES land uses only"),
        "Proposed_RES_Erf_Sz": st.column_config.NumberColumn(min_value=0.0, help="RES land uses only"),
        "Parking_Zone": st.column_config.SelectboxColumn(options=PARKING_ZONE_OPTIONS),
        "DUs_Fix_Actual": st.column_config.NumberColumn(min_value=0, help="Overrides calculated yield if provided"),
        "GLA_Fix_Actual": st.column_config.NumberColumn(min_value=0.0, help="Overrides calculated yield if provided"),
    },
    use_container_width=True,
)
st.caption(
    "RES_Avg_Unit_Size / RES_Avg_Value_ZAR / Proposed_RES_Erf_Sz only apply to residential rows - "
    "leave blank for non-residential land uses. Not enforced automatically yet; "
    "add a row-level check here if stray values on NRES rows become a problem in practice."
)

attrs = {
    "Site_ID": site_id,
    "District_Code": district_code,
    "Site_Extent_SQM": site_extent_sqm,
    "Zoning_Code": zoning_code,
    "Dev_Type": dev_type,
    "Review_Category": review_category,
    "Update_Reason": update_reason,
    "Update_Notes": update_notes,
    "LIS_IDs": lis_ids,
    "Development_Location_Issue": development_location_issue,
    "SDP_Available": sdp_available,
    "Perc_Complete_around_2030": pct_2030,
    "Perc_Complete_by_2050": pct_2050,
    "Perc_Complete_beyond_2050": pct_beyond_2050,
    "Certainty_Use": certainty_use,
    "Certainty_Intensity": certainty_intensity,
    "Certainty_TimeFrame": certainty_timeframe,
    "Review_Notes": review_notes,
}
if mode == "Edit existing site" and site_attrs:
    attrs["Previous_Update_Category"] = site_attrs.get("Review_Category")
    attrs["Previous_Update_Reason"] = site_attrs.get("Update_Reason")
    attrs["Previous_Update_Notes"] = site_attrs.get("Update_Notes")

# A boundary is only being changed if we're in "Draw new site" mode, or in
# "Edit existing site" mode with the adjust-boundary toggle on AND
# something has actually been drawn.
boundary_changing = mode == "Draw new site" or (
    mode == "Edit existing site" and st.session_state.adjust_boundary
    and st.session_state.drawn_geometry is not None
)

current_hash = hashlib.sha256(
    json.dumps(
        {
            "attrs": attrs,
            "land_uses": edited_land_uses.astype(str).values.tolist(),
            "boundary_changing": boundary_changing,
            "mode": mode,
        },
        sort_keys=True, default=str,
    ).encode()
).hexdigest()

review_col, confirm_col = st.columns(2)

with review_col:
    if st.button("Review changes"):
        errors = validate_land_uses(edited_land_uses, zoning_code, review_category)
        errors += validate_timeframes(pct_2030, pct_2050, pct_beyond_2050)
        if not site_id or not site_extent_sqm:
            errors.append("Site_ID and Site_Extent_SQM are required.")
        if mode == "Draw new site" and st.session_state.drawn_geometry is None:
            errors.append("Draw a site boundary before saving.")
        if mode == "Edit existing site" and st.session_state.adjust_boundary \
                and st.session_state.drawn_geometry is None:
            errors.append("Draw the replacement boundary, or untick 'Adjust this site's boundary'.")

        if errors:
            for e in errors:
                st.error(e)
            st.session_state.reviewed_hash = None
        else:
            diff = []
            if mode == "Edit existing site" and site_attrs:
                diff = diff_site_attrs(site_attrs, attrs)
                diff_msgs = diff_land_uses(st.session_state.land_use_df, edited_land_uses)
                if boundary_changing:
                    diff_msgs.append("Site boundary will be replaced with the newly drawn shape.")
            else:
                diff_msgs = ["New site - no prior state to compare."]

            st.session_state.reviewed_hash = current_hash
            st.session_state.reviewed_diff = (diff, diff_msgs)

if st.session_state.reviewed_hash == current_hash and st.session_state.reviewed_diff:
    diff, diff_msgs = st.session_state.reviewed_diff
    st.subheader("Review before saving")
    if diff:
        st.dataframe(
            pd.DataFrame(diff, columns=["Field", "Current value", "New value"]),
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("No attribute changes detected.")
    for m in diff_msgs:
        st.write(f"- {m}")
    if not diff and not diff_msgs:
        st.info("No changes detected - nothing to save.")

    with confirm_col:
        if st.button("Confirm & Save", type="primary"):
            # Re-check for staleness right before writing - guards against
            # someone else editing this site between load and save.
            if mode == "Edit existing site" and st.session_state.existing_global_id:
                if not check_not_stale(
                    sites_layer, st.session_state.existing_global_id, st.session_state.loaded_edit_date
                ):
                    st.error(
                        "This site was edited by someone else since you loaded it. "
                        "Reload the site to see the latest version before saving."
                    )
                    st.stop()

            geometry = None
            if boundary_changing:
                sr = sites_layer.properties.extent["spatialReference"]
                geometry = geojson_draw_to_esri_geometry(st.session_state.drawn_geometry, sr)

            try:
                write_site_and_land_uses(
                    sites_layer, land_uses_table, attrs, geometry,
                    edited_land_uses, st.session_state.existing_global_id,
                )
                st.success(f"Saved {site_id}.")
                st.session_state.reviewed_hash = None
            except PartialSaveError as exc:
                # Something committed, something didn't. Store the GlobalID
                # so the *next* click retries as an update, not a duplicate add.
                st.session_state.existing_global_id = exc.site_global_id
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"Save failed - nothing was changed: {exc}")
else:
    with confirm_col:
        st.button("Confirm & Save", type="primary", disabled=True,
                   help="Click 'Review changes' first.")
    if st.session_state.reviewed_hash is not None:
        st.caption("Form changed since last review - click 'Review changes' again.")
