/* Lon/lat <-> scene-metre conversion for the browser, and draping things onto the terrain.
 *
 * The server places every point with pyproj in the scene frame's own CRS (scene.frame_crs: polar stereographic
 * EPSG:3413 / EPSG:3031 near the poles, a per-scene azimuthal equidistant elsewhere), then subtracts origin_xy. The
 * browser has to use the SAME projection for anything it places itself -- the H3 grid, the lat/lon graticule, map
 * markers -- and to read a point's position back for a hover readout. These used to be a linear approximation around
 * the bbox centre with the EQUATORIAL length of a degree of latitude, which put the grid 68 m off the points 11 km
 * from a Jakobshavn scene's centre and 195 m off at 17 km. proj4 with the frame's definition matches pyproj to well
 * under a millimetre (tests/test_scene_geo.js).
 */
window.AICESAT = window.AICESAT || {};
(function () {
  // pyproj names the polar frames by EPSG code, which proj4 does not ship; these are their PROJ definitions.
  const PROJ_DEFS = {
    'EPSG:3413': '+proj=stere +lat_0=90 +lat_ts=70 +lon_0=-45 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs',
    'EPSG:3031': '+proj=stere +lat_0=-90 +lat_ts=-71 +lon_0=0 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs',
  };
  const cache = new Map();

  function frameProj(fr) {
    if (!fr || !fr.crs || !fr.origin_xy || typeof proj4 === 'undefined') return null;
    let p = cache.get(fr.crs);
    if (!p) {
      // proj4 does not know PROJ's +type=crs, and returns NaN for aeqd unless the false easting/northing are given;
      // both are no-ops for pyproj, so the definition means the same thing on both sides.
      let def = PROJ_DEFS[fr.crs] || fr.crs.replace(/\s*\+type=crs\b/, '');
      if (!/\+x_0=/.test(def)) def += ' +x_0=0 +y_0=0';
      p = proj4('EPSG:4326', def);
      cache.set(fr.crs, p);
    }
    return p;
  }

  // ---- fallback only: a frame with no CRS (the unit tests' synthetic frames) ------------------------------------
  const M_PER_DEG_LAT = 110574;
  function frameCentre(fr) { const b = fr.bbox; return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]; }
  function mPerDegLon(clat) { return 111320 * Math.cos(clat * Math.PI / 180); }

  function localToLonLat(fr, x, y) {
    const p = frameProj(fr);
    if (p) return p.inverse([x + fr.origin_xy[0], y + fr.origin_xy[1]]);
    const E = fr.east_xy || [1, 0], N = fr.north_xy || [0, 1];
    const [clon, clat] = frameCentre(fr);
    // SOLVE [E N][de dn]' = [x y]', do not project: E and N are only approximately orthonormal (finite differences
    // rounded to 6 decimals), so a dot-product inverse drifts with distance from the centre. A 2x2 solve is exact.
    const det = E[0] * N[1] - N[0] * E[1];
    if (!det) return [clon, clat];                     // degenerate basis: refuse to invent a coordinate
    const de = (N[1] * x - N[0] * y) / det, dn = (E[0] * y - E[1] * x) / det;
    return [clon + de / mPerDegLon(clat), clat + dn / M_PER_DEG_LAT];
  }

  function lonLatToLocal(fr, lon, lat) {
    const p = frameProj(fr);
    if (p) { const [x, y] = p.forward([lon, lat]); return [x - fr.origin_xy[0], y - fr.origin_xy[1]]; }
    const E = fr.east_xy || [1, 0], N = fr.north_xy || [0, 1];
    const [clon, clat] = frameCentre(fr);
    const de = (lon - clon) * mPerDegLon(clat), dn = (lat - clat) * M_PER_DEG_LAT;
    return [de * E[0] + dn * N[0], de * E[1] + dn * N[1]];
  }

  // A vertex draped onto the terrain: the ground height there plus a lift, or `fallback` over a DEM hole. Anything
  // meant to read as "on the ground" (the H3 grid, time-series cell outlines) uses this, so they cannot disagree
  // about where the ground is.
  function drapedZ(heightAt, x, y, fallback, lift) {
    const h = heightAt(x, y);
    return (h == null || !isFinite(h) ? fallback : h) + (lift || 0);
  }

  AICESAT.geo = {frameProj, frameCentre, mPerDegLon, M_PER_DEG_LAT, localToLonLat, lonLatToLocal, drapedZ};
})();
