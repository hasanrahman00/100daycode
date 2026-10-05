"""Sanity-check the place and domain counts against the raw Foursquare data.

Usage:
    python check_counts.py
"""
import duckdb

con = duckdb.connect()
places = "data/release/*/places/parquet/*.parquet"

print("Raw Foursquare data:")
print(con.sql(f"""
    SELECT count(*)                       AS all_places,
           count(website)                 AS places_with_website,
           count(DISTINCT lower(website)) AS unique_raw_websites
    FROM '{places}'
"""))

print("domains.csv:")
print(con.sql("""
    SELECT count(*) AS unique_domains, sum(places) AS places_covered
    FROM read_csv('output/domains.csv', header=true, quote='"')
"""))

print("Biggest domains (chains share one website):")
print(con.sql("""
    SELECT domain, country, places
    FROM read_csv('output/domains.csv', header=true, quote='"')
    ORDER BY places DESC LIMIT 15
"""))
