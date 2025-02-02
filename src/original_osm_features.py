"""
Note: For resource constrained (typically memory) environments you should use osm_features.py instead. If you are sure you have enough memory (e.g., HPC environment with 64gb+)
      then this script may produce results faster. However, parallelization and optimization of osm_features.py may make that a more efficient option in any case. This script
      will not likely be updated further.

python 3.9

portions of code and methodology based on https://github.com/thinkingmachines/ph-poverty-mapping


Extract features features OSM data

download OSM data from
http://download.geofabrik.de/asia/philippines.html#


buildings (polygons)
types : residential, damaged, commercial, industrial, education, health
For each type, we calculated
    - the total number of buildings (count poly features intersecting with buffer)
    - the total area of buildings (sum of area of poly features which intersect with buffer)
    - the mean area of buildings (avg area of poly features which intersect with buffer)
    - the proportion of the cluster area occupied by the buildings (ratio of total area of buildings which intersect with buffer to buffer area)

pois (points)
types: 100+ different types
For each type, we calculated
    - the total number of each POI within a proximity of the area (point in poly)

roads (lines)
types: primary, trunk, paved, unpaved, intersection
for each type of road, we calculated
    - the distance to the closest road (point to line vertice dist)
    - total number of roads (count line features which intersect with buffer)
    - total road length (length of lines which intersect with buffer)

"""

import os
import configparser

import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from sklearn.neighbors import BallTree


if 'config.ini' not in os.listdir():
    raise FileNotFoundError("config.ini file not found. Make sure you run this from the root directory of the repo.")

config = configparser.ConfigParser()
config.read('config.ini')


project = config["main"]["project"]
project_dir = config["main"]["project_dir"]

dhs_round = config[project]['dhs_round']
country_utm_epsg_code = config[project]['country_utm_epsg_code']

country_name = config[project]["country_name"]
osm_date = config[project]["osm_date"]
geom_id = config[project]["geom_id"]
geom_label = config[project]["geom_label"]


data_dir = os.path.join(project_dir, 'data')

osm_features_dir = os.path.join(data_dir, 'outputs', dhs_round, 'osm_features')
os.makedirs(osm_features_dir, exist_ok=True)



# DHS CLUSTERS

# load buffers/geom created during data prep
geom_path = os.path.join(data_dir, 'outputs', dhs_round, 'dhs_buffers.geojson')
buffers_gdf = gpd.read_file(geom_path)

# calculate area of each buffer
# convert to UTM first, then back to WGS84 (degrees)
buffers_gdf = buffers_gdf.to_crs(f"EPSG:{country_utm_epsg_code}")
buffers_gdf["buffer_area"] = buffers_gdf.area
buffers_gdf['longitude'] = buffers_gdf.centroid.x
buffers_gdf['latitude'] = buffers_gdf.centroid.y
buffers_gdf = buffers_gdf.to_crs("EPSG:4326") # WGS84


# ---------------------------------------------------------
# pois
# count of each type of pois (100+) in each buffer

print("Running pois...")

osm_pois_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_pois_free_1.shp')
osm_pois_a_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_pois_a_free_1.shp')

raw_pois_geo = gpd.read_file(osm_pois_shp_path)
raw_pois_a_geo = gpd.read_file(osm_pois_a_shp_path)

pois_geo_raw = pd.concat([raw_pois_geo, raw_pois_a_geo])

# load crosswalk for types and assign any not grouped to "other"
pois_type_crosswalk_path = os.path.join(data_dir, 'crosswalks/pois_type_crosswalk.csv')
pois_type_crosswalk_df = pd.read_csv(pois_type_crosswalk_path)
pois_type_crosswalk_df.loc[pois_type_crosswalk_df["group"] == "0", "group"] = "other"

# merge new classification and assign any features without a type to unclassifid
pois_geo = pois_geo_raw.merge(pois_type_crosswalk_df, left_on="fclass", right_on="type", how="left")

pois_geo.loc[pois_geo["fclass"].isna(), "group"] = "unclassified"

# show breakdown of groups
print(pois_geo.group.value_counts())

# group_field = "fclass"
group_field = "group"

# split by group
# pois_group_list = ["all"] + [i for i in set(pois_geo[group_field])]
pois_group_list = [i for i in set(pois_geo[group_field]) if pd.notnull(i)]

# copy of buffers gdf to use for output
buffers_gdf_pois = buffers_gdf.copy(deep=True)

for group in pois_group_list:
    print(group)
    # subet by group
    if group == "all":
        pois_geo_subset = pois_geo.reset_index(inplace=True).copy(deep=True)
    else:
        pois_geo_subset = pois_geo.loc[pois_geo[group_field] == group].reset_index().copy(deep=True)
    # query to find pois in each buffer
    bquery = pois_geo_subset.sindex.query_bulk(buffers_gdf.geometry)
    # pois dataframe where each column contains a cluster and one building in it (can have multiple rows per cluster)
    bquery_df = pd.DataFrame({"cluster": bquery[0], "pois": bquery[1]})
    # add pois data to spatial query dataframe
    bquery_full = bquery_df.merge(pois_geo_subset, left_on="pois", right_index=True, how="left")
    # aggregate spatial query df with pois info, by cluster
    bquery_agg = bquery_full.groupby("cluster").agg({"pois": "count"})
    bquery_agg.columns = [group + "_pois_count"]
    # join cluster back to original buffer_geo dataframe with columns for specific building type queries
    z1 = buffers_gdf.merge(bquery_agg, left_index=True, right_on="cluster", how="left")
    # not each cluster will have relevant pois, set those to zero
    z1.fillna(0, inplace=True)
    # set index and drop unnecessary columns
    if z1.index.name != "cluster": z1.set_index("cluster", inplace=True)
    z2 = z1[group + "_pois_count"]
    # merge group columns back to main cluster dataframe
    buffers_gdf_pois = buffers_gdf_pois.merge(z2, left_index=True, right_index=True)

# output final features
pois_feature_cols = [i for i in buffers_gdf_pois.columns if "_pois_" in i]
pois_cols = [geom_id] + pois_feature_cols
pois_features = buffers_gdf_pois[pois_cols].copy(deep=True)
pois_features['all_pois_count'] = pois_features[pois_feature_cols].sum(axis=1)
pois_features_path = os.path.join(osm_features_dir, '{}_pois_{}.csv'.format(geom_label, osm_date))
pois_features.to_csv(pois_features_path, index=False, encoding="utf-8")


# ---------------------------------------------------------
# traffic
# count of each type of traffic item in each buffer

print("Running traffic...")

osm_traffic_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_traffic_free_1.shp')
osm_traffic_a_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_traffic_a_free_1.shp')

raw_traffic_geo = gpd.read_file(osm_traffic_shp_path)
raw_traffic_a_geo = gpd.read_file(osm_traffic_a_shp_path)

traffic_geo_raw = pd.concat([raw_traffic_geo, raw_traffic_a_geo])

# load crosswalk for types and assign any not grouped to "other"
traffic_type_crosswalk_path = os.path.join(data_dir, 'crosswalks/traffic_type_crosswalk.csv')
traffic_type_crosswalk_df = pd.read_csv(traffic_type_crosswalk_path)
traffic_type_crosswalk_df.loc[traffic_type_crosswalk_df["group"] == "0", "group"] = "other"

# merge new classification and assign any features without a type to unclassifid
traffic_geo = traffic_geo_raw.merge(traffic_type_crosswalk_df, left_on="fclass", right_on="type", how="left")

traffic_geo.loc[traffic_geo["fclass"].isna(), "group"] = "unclassified"

# show breakdown of groups
print(traffic_geo.group.value_counts())

# group_field = "fclass"
group_field = "group"

# split by group
# traffic_group_list = ["all"] + [i for i in set(traffic_geo[group_field])]
traffic_group_list = [i for i in set(traffic_geo[group_field]) if pd.notnull(i)]

# copy of buffers gdf to use for output
buffers_gdf_traffic = buffers_gdf.copy(deep=True)

for group in traffic_group_list:
    print(group)
    # subet by group
    if group == "all":
        traffic_geo_subset = traffic_geo.copy(deep=True)
    else:
        traffic_geo_subset = traffic_geo.loc[traffic_geo[group_field] == group].reset_index().copy(deep=True)
    # query to find traffic in each buffer
    bquery = traffic_geo_subset.sindex.query_bulk(buffers_gdf.geometry)
    # traffic dataframe where each column contains a cluster and one building in it (can have multiple rows per cluster)
    bquery_df = pd.DataFrame({"cluster": bquery[0], "traffic": bquery[1]})
    # add traffic data to spatial query dataframe
    bquery_full = bquery_df.merge(traffic_geo_subset, left_on="traffic", right_index=True, how="left")
    # aggregate spatial query df with traffic info, by cluster
    bquery_agg = bquery_full.groupby("cluster").agg({"traffic": "count"})
    bquery_agg.columns = [group + "_traffic_count"]
    # join cluster back to original buffer_geo dataframe with columns for specific building type queries
    z1 = buffers_gdf.merge(bquery_agg, left_index=True, right_on="cluster", how="left")
    # not each cluster will have relevant traffic, set those to zero
    z1.fillna(0, inplace=True)
    # set index and drop unnecessary columns
    if z1.index.name != "cluster": z1.set_index("cluster", inplace=True)
    z2 = z1[group + "_traffic_count"]
    # merge group columns back to main cluster dataframe
    buffers_gdf_traffic = buffers_gdf_traffic.merge(z2, left_index=True, right_index=True)

# output final features
traffic_feature_cols = [i for i in buffers_gdf_traffic.columns if "_traffic_" in i]
traffic_cols = [geom_id] + traffic_feature_cols
traffic_features = buffers_gdf_traffic[traffic_cols].copy(deep=True)
traffic_features['all_traffic_count'] = traffic_features[traffic_feature_cols].sum(axis=1)
traffic_features_path = os.path.join(osm_features_dir, '{}_traffic_{}.csv'.format(geom_label, osm_date))
traffic_features.to_csv(traffic_features_path, index=False, encoding="utf-8")

# ---------------------------------------------------------
# transport
# count of each type of transport item in each buffer

print("Running transport...")

osm_transport_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_transport_free_1.shp')
osm_transport_a_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_transport_a_free_1.shp')

raw_transport_geo = gpd.read_file(osm_transport_shp_path)
raw_transport_a_geo = gpd.read_file(osm_transport_a_shp_path)

transport_geo_raw = pd.concat([raw_transport_geo, raw_transport_a_geo])

# load crosswalk for types and assign any not grouped to "other"
transport_type_crosswalk_path = os.path.join(data_dir, 'crosswalks/transport_type_crosswalk.csv')
transport_type_crosswalk_df = pd.read_csv(transport_type_crosswalk_path)
transport_type_crosswalk_df.loc[transport_type_crosswalk_df["group"] == "0", "group"] = "other"

# merge new classification and assign any features without a type to unclassifid
transport_geo = transport_geo_raw.merge(transport_type_crosswalk_df, left_on="fclass", right_on="type", how="left")

transport_geo.loc[transport_geo["fclass"].isna(), "group"] = "unclassified"

# show breakdown of groups
print(transport_geo.group.value_counts())

# group_field = "fclass"
group_field = "group"

# split by group
# transport_group_list = ["all"] + [i for i in set(transport_geo[group_field])]
transport_group_list = [i for i in set(transport_geo[group_field]) if pd.notnull(i)]

# copy of buffers gdf to use for output
buffers_gdf_transport = buffers_gdf.copy(deep=True)

for group in transport_group_list:
    print(group)
    # subet by group
    if group == "all":
        transport_geo_subset = transport_geo.copy(deep=True)
    else:
        transport_geo_subset = transport_geo.loc[transport_geo[group_field] == group].reset_index().copy(deep=True)
    # query to find transport in each buffer
    bquery = transport_geo_subset.sindex.query_bulk(buffers_gdf.geometry)
    # transport dataframe where each column contains a cluster and one building in it (can have multiple rows per cluster)
    bquery_df = pd.DataFrame({"cluster": bquery[0], "transport": bquery[1]})
    # add transport data to spatial query dataframe
    bquery_full = bquery_df.merge(transport_geo_subset, left_on="transport", right_index=True, how="left")
    # aggregate spatial query df with transport info, by cluster
    bquery_agg = bquery_full.groupby("cluster").agg({"transport": "count"})
    bquery_agg.columns = [group + "_transport_count"]
    # join cluster back to original buffer_geo dataframe with columns for specific building type queries
    z1 = buffers_gdf.merge(bquery_agg, left_index=True, right_on="cluster", how="left")
    # not each cluster will have relevant transport, set those to zero
    z1.fillna(0, inplace=True)
    # set index and drop unnecessary columns
    if z1.index.name != "cluster": z1.set_index("cluster", inplace=True)
    z2 = z1[group + "_transport_count"]
    # merge group columns back to main cluster dataframe
    buffers_gdf_transport = buffers_gdf_transport.merge(z2, left_index=True, right_index=True)

# output final features
transport_feature_cols = [i for i in buffers_gdf_transport.columns if "_transport_" in i]
transport_cols = [geom_id] + transport_feature_cols
transport_features = buffers_gdf_transport[transport_cols].copy(deep=True)
transport_features['all_transport_count'] = transport_features[transport_feature_cols].sum(axis=1)
transport_features_path = os.path.join(osm_features_dir, '{}_transport_{}.csv'.format(geom_label, osm_date))
transport_features.to_csv(transport_features_path, index=False, encoding="utf-8")


# ---------------------------------------------------------
# # buildings
# # for each type of building (and all buildings combined)
# # count of buildings in each buffer, average areas of buildings in each buffer, total area of building in each buffer, ratio of building area to total area of buffer

print("Running buildings...")

osm_buildings_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_buildings_a_free_1.shp')
buildings_geo_raw = gpd.read_file(osm_buildings_shp_path)

# load crosswalk for building types and assign any not grouped to "other"
building_type_crosswalk_path = os.path.join(data_dir, 'crosswalks/buildings_type_crosswalk.csv')
building_type_crosswalk_df = pd.read_csv(building_type_crosswalk_path)
building_type_crosswalk_df.loc[building_type_crosswalk_df["group"] == "0", "group"] = "other"

# merge new classification and assign any buildings without a type to unclassifid
buildings_geo = buildings_geo_raw.merge(building_type_crosswalk_df, on="type", how="left")

buildings_geo.loc[buildings_geo["type"].isna(), "group"] = "unclassified"

group_field = "group"

# # show breakdown of groups
print(buildings_geo.group.value_counts())


# split by building types
# group_list = ["residential"]
# group_list = ["all"] + [i for i in set(buildings_geo["group"]) if i not in ["other", "unclassified"]]
buildings_group_list = [i for i in set(buildings_geo["group"]) if i not in ["other", "unclassified"]]

buildings_group_list = [i for i in buildings_group_list if str(i) != 'nan']  #removes nan from building_group_list - Sasan

buildings_group_list = buildings_group_list #+ ['all'] #add a section for all buildings into group list



if "all" not in buildings_group_list:
    buildings_geo = buildings_geo.loc[buildings_geo["group"].isin(buildings_group_list)]

# calculate area of each building
# convert to UTM first, then back to WGS84 (degrees)
buildings_geo = buildings_geo.to_crs(f"EPSG:{country_utm_epsg_code}")
buildings_geo["area"] = buildings_geo.area
buildings_geo = buildings_geo.to_crs("EPSG:4326") # WGS84


# copy of buffers gdf to use for output
buffers_gdf_buildings = buffers_gdf.copy(deep=True)

for group in buildings_group_list:
    print(group)
    # subet by group
    if group == "all":
        buildings_geo_subset = buildings_geo.copy(deep=True)
    else:
        buildings_geo_subset = buildings_geo.loc[buildings_geo[group_field] == group].reset_index().copy(deep=True)
    # query to find buildings in each buffer
    bquery = buildings_geo_subset.sindex.query_bulk(buffers_gdf.geometry)
    # building dataframe where each column contains a cluster and one building in it (can have multiple rows per cluster)
    bquery_df = pd.DataFrame({"cluster": bquery[0], "building": bquery[1]})
    # add building data to spatial query dataframe
    bquery_full = bquery_df.merge(buildings_geo_subset, left_on="building", right_index=True, how="left")
    # aggregate spatial query df with building info, by cluster
    bquery_agg = bquery_full.groupby("cluster").agg({
        "area": ["count", "mean", "sum"]
    })
    # rename agg df
    basic_building_cols = ["buildings_count", "buildings_avgarea", "buildings_totalarea"]
    bquery_agg.columns = ["{}_{}".format(group, i) for i in basic_building_cols]
    # join cluster back to original buffer_geo dataframe with columns for specific building type queries
    z1 = buffers_gdf.merge(bquery_agg, left_index=True, right_on="cluster", how="left")
    # not each cluster will have relevant buildings, set those to zero
    z1.fillna(0, inplace=True)
    # calculate ratio for building type
    z1["{}_buildings_ratio".format(group)] = z1["{}_buildings_totalarea".format(group)] / z1["buffer_area"]
    # set index and drop unnecessary columns
    if z1.index.name != "cluster": z1.set_index("cluster", inplace=True)
    z2 = z1[bquery_agg.columns.to_list() + ["{}_buildings_ratio".format(group)]]
    # merge group columns back to main cluster dataframe
    buffers_gdf_buildings = buffers_gdf_buildings.merge(z2, left_index=True, right_index=True)


# output final features
buildings_feature_cols = [i for i in buffers_gdf_buildings.columns if "_buildings_" in i]
buildings_cols = [geom_id] + buildings_feature_cols
buildings_features = buffers_gdf_buildings[buildings_cols].copy(deep=True)

# buildings_features = pd.read_csv(buildings_features_path)
# buildings_feature_cols = buildings_features.columns.to_list()

if 'all' not in buildings_group_list:
    buildings_features["all_buildings_count"] = buildings_features[[i for i in buildings_feature_cols if i.endswith('_buildings_count')]].sum(axis=1)
    buildings_features["all_buildings_totalarea"] = buildings_features[[i for i in buildings_feature_cols if i.endswith('_buildings_totalarea')]].sum(axis=1)
    buildings_features["all_buildings_avgarea"] = buildings_features["all_buildings_totalarea"] / buildings_features["all_buildings_count"]
    buildings_features["all_buildings_avgarea"].fillna(0, inplace=True)
    buildings_features = buildings_features.merge(buffers_gdf[[geom_id, 'buffer_area']], on=geom_id, how="left")
    buildings_features["all_buildings_ratio"] = buildings_features["all_buildings_totalarea"] / buildings_features["buffer_area"]


buildings_features_path = os.path.join(osm_features_dir, f'{geom_label}_buildings_{osm_date}.csv')
buildings_features.to_csv(buildings_features_path, index=False, encoding="utf-8")


# ---------------------------------------------------------
# roads
# for each type of road
# distance to closest road from cluster centroid, total number of roads in each cluster, and total length of roads in each cluster

print("Running roads...")

osm_roads_shp_path = os.path.join(data_dir, f'osm/{country_name}-{osm_date}-free.shp/gis_osm_roads_free_1.shp')
roads_raw_geo = gpd.read_file(osm_roads_shp_path)

# get each road length
# convert to UTM first, then back to WGS84 (degrees)
roads_raw_geo = roads_raw_geo.to_crs(f"EPSG:{country_utm_epsg_code}")
roads_raw_geo["road_length"] = roads_raw_geo.geometry.length
roads_raw_geo = roads_raw_geo.to_crs("EPSG:4326") # WGS84

# load crosswalk for types and assign any not grouped to "other"
roads_type_crosswalk_path = os.path.join(data_dir, 'crosswalks/roads_type_crosswalk.csv')
roads_type_crosswalk_df = pd.read_csv(roads_type_crosswalk_path)
roads_type_crosswalk_df.loc[roads_type_crosswalk_df["group"] == "0", "group"] = "other"

# merge new classification and assign any features without a type to unclassifid
roads_geo = roads_raw_geo.merge(roads_type_crosswalk_df, left_on="fclass", right_on="type", how="left")

roads_geo.loc[roads_geo["fclass"].isna(), "group"] = "unclassified"

# group_field = "fclass"
group_field = "group"

# show breakdown of groups
print(roads_geo[group_field].value_counts())


# split by groups
min_road_features = 0 # 1000
roads_group_list = [i for i,j in roads_geo[group_field].value_counts().to_dict().items() if j > min_road_features]
# roads_group_list = ["all"] + [i for i,j in roads_geo[group_field].value_counts().to_dict().items() if j > 1000]
# roads_group_list = ["all"] + [i for i in set(roads_geo["fclass"])]
# roads_group_list = ["all", "primary", "secondary"]


#-----------------
#find distance to nearest road (based on vertices of roads)


cluster_centroids = buffers_gdf.copy(deep=True)

src_points = cluster_centroids.apply(lambda x: (x.longitude, x.latitude), axis=1).to_list()


for group in roads_group_list:
    print(group)
    # subset based on group
    if group == "all":
        subset_roads_geo = roads_geo.copy(deep=True)
    else:
        subset_roads_geo = roads_geo.loc[roads_geo[group_field] == group].reset_index().copy(deep=True)
    # generate list of all road vertices and convert to geodataframe
    line_xy = subset_roads_geo.apply(lambda x: (x.osm_id, x.geometry.xy), axis=1)
    line_xy_lookup = [j for i in line_xy for j in list(zip([i[0]]*len(i[1][0]), *i[1]))]
    line_xy_df = pd.DataFrame(line_xy_lookup, columns=["osm_id", "x", "y"])
    line_xy_points = [(i[1], i[2]) for i in line_xy_lookup]
    # create ball tree for nearest point lookup
    #  see https://automating-gis-processes.github.io/site/notebooks/L3/nearest-neighbor-faster.html
    tree = BallTree(line_xy_points, leaf_size=50, metric='haversine')
    # query tree
    distances, indices = tree.query(src_points, k=1)
    distances = distances.transpose()
    indices = indices.transpose()
    # k=1 so output length is array of len=1
    closest = indices[0]
    closest_dist = distances[0]
    # def func to get osm id for closest locations
    osm_id_lookup = lambda idx: line_xy_df.loc[idx].osm_id
    # set final data
    cluster_centroids["{}_roads_nearest-osmid".format(group)] = list(map(osm_id_lookup, closest))
    cluster_centroids["{}_roads_nearestdist".format(group)] = closest_dist



cluster_centroids = cluster_centroids[[geom_id] + [i for i in cluster_centroids.columns if "_roads_" in i]]
cluster_centroids.set_index(geom_id, inplace=True)


# # -----------------
# # calculate number of roads and length of roads intersecting with each buffer

# # copy of buffers gdf to use for output
buffers_gdf_roads = buffers_gdf.copy(deep=True)

for group in roads_group_list:
    print(group)
    if group == "all":
        subset_roads_geo = roads_geo.copy(deep=True)
    else:
        subset_roads_geo = roads_geo.loc[roads_geo[group_field] == group].reset_index().copy(deep=True)
    # query to find roads in each buffer
    bquery = subset_roads_geo.sindex.query_bulk(buffers_gdf.geometry)
    # roads dataframe where each column contains a cluster and one building in it (can have multiple rows per cluster)
    bquery_df = pd.DataFrame({"cluster": bquery[0], "roads": bquery[1]})
    # add roads data to spatial query dataframe
    bquery_full = bquery_df.merge(roads_geo, left_on="roads", right_index=True, how="left")
    # aggregate spatial query df with roads info, by cluster
    bquery_agg = bquery_full.groupby("cluster").agg({"road_length": ["count", "sum"]})
    bquery_agg.columns = [group + "_roads_count", group + "_roads_length"]
    # join cluster back to original buffer_geo dataframe with columns for specific building type queries
    z1 = buffers_gdf.merge(bquery_agg, left_index=True, right_on="cluster", how="left")
    # not each cluster will have relevant roads, set those to zero
    z1.fillna(0, inplace=True)
    # set index and drop unnecessary columns
    if z1.index.name != "cluster": z1.set_index("cluster", inplace=True)
    z2 = z1[[group + "_roads_count", group + "_roads_length"]]
    # merge group columns back to main cluster dataframe
    buffers_gdf_roads = buffers_gdf_roads.merge(z2, left_index=True, right_index=True)


# output final features
roads_features = buffers_gdf_roads.merge(cluster_centroids, on=geom_id)
roads_feature_cols = [i for i in roads_features.columns if "_roads_" in i]
roads_cols = [geom_id] + roads_feature_cols
roads_features = roads_features[roads_cols].copy(deep=True)

roads_features['all_roads_length'] = roads_features[[i for i in roads_feature_cols if i.endswith("_roads_length")]].sum(axis=1)
roads_features['all_roads_count'] = roads_features[[i for i in roads_feature_cols if i.endswith("_roads_count")]].sum(axis=1)
roads_features['all_roads_nearestdist'] = roads_features[[i for i in roads_feature_cols if i.endswith("_roads_nearestdist")]].min(axis=1)
# roads_features['all_roads_nearest-osmid'] =

roads_features_path = os.path.join(osm_features_dir, '{}_roads_{}.csv'.format(geom_label, osm_date))
roads_features.to_csv(roads_features_path, index=False, encoding="utf-8")
