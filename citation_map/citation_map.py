# Copyright (c) 2024 Chen Liu
# Modified to include journal/venue information
# All rights reserved.
import folium
import itertools
import pandas as pd
import os
import pickle
import pycountry
import re
import random
import time

from geopy.geocoders import Nominatim
from multiprocessing import Pool
from scholarly import scholarly, ProxyGenerator
from tqdm import tqdm
from typing import Any, List, Tuple, Optional

from .scholarly_support import get_citing_author_ids_and_citing_papers, get_organization_name, NO_AUTHOR_FOUND_STR, KNOWN_AFFILIATION_DICT

# For backward compatibility if venue is not present
NO_VENUE_FOUND_STR = ''


def find_all_citing_authors(scholar_id: str, num_processes: int = 16) -> List[Tuple[str]]:
    '''
    Step 1. Find all publications of the given Google Scholar ID.
    Step 2. Find all citing authors (now includes venue information).
    
    Returns list of tuples: (author_id, citing_paper_title, cited_paper_title, venue)
    '''
    # Find Google Scholar Profile using Scholar ID.
    author = scholarly.search_author_id(scholar_id)
    author = scholarly.fill(author, sections=['publications'])
    publications = author['publications']
    print('Author profile found, with %d publications.\n' % len(publications))

    # Fetch metadata for all publications.
    if isinstance(num_processes, int) and num_processes > 1:
        with Pool(processes=num_processes) as pool:
            all_publications = list(tqdm(pool.imap(__fill_publication_metadata, publications),
                                         desc='Filling metadata for your %d publications' % len(publications),
                                         total=len(publications)))
    else:
        all_publications = []
        for pub in tqdm(publications,
                        desc='Filling metadata for your %d publications' % len(publications),
                        total=len(publications)):
            all_publications.append(__fill_publication_metadata(pub))

    # Convert all publications to Google Scholar publication IDs and paper titles.
    # This is fast and no parallel processing is needed.
    all_publication_info = []
    for pub in all_publications:
        if 'cites_id' in pub:
            for cites_id in pub['cites_id']:
                pub_title = pub['bib']['title']
                all_publication_info.append((cites_id, pub_title))

    # Find all citing authors from all publications.
    # To best solve CAPTCHA problems, we won't perform parallel processing here.
    all_citing_author_paper_info_nested = []
    for pub in tqdm(all_publication_info,
                    desc='Finding citing authors and papers on your %d publications' % len(all_publication_info),
                    total=len(all_publication_info)):
        all_citing_author_paper_info_nested.append(__citing_authors_and_papers_from_publication(pub))
    all_citing_author_paper_tuple_list = list(itertools.chain(*all_citing_author_paper_info_nested))
    return all_citing_author_paper_tuple_list

def find_all_citing_affiliations(all_citing_author_paper_tuple_list: List[Tuple[str]],
                                 num_processes: int = 16,
                                 affiliation_conservative: bool = False):
    '''
    Step 3. Find all citing affiliations.
    
    Now handles tuples with venue: (author_id, citing_paper_title, cited_paper_title, venue)
    Returns: (author_name, citing_paper_title, cited_paper_title, venue, affiliation)
    '''
    if affiliation_conservative:
        __affiliations_from_authors = __affiliations_from_authors_conservative
    else:
        __affiliations_from_authors = __affiliations_from_authors_aggressive

    # Find all citing insitutions from all citing authors.
    if num_processes > 1 and isinstance(num_processes, int):
        with Pool(processes=num_processes) as pool:
            author_paper_affiliation_tuple_list = list(tqdm(pool.imap(__affiliations_from_authors, all_citing_author_paper_tuple_list),
                                                            desc='Finding citing affiliations from %d citing authors' % len(all_citing_author_paper_tuple_list),
                                                            total=len(all_citing_author_paper_tuple_list)))
    else:
        author_paper_affiliation_tuple_list = []
        for author_and_paper in tqdm(all_citing_author_paper_tuple_list,
                                     desc='Finding citing affiliations from %d citing authors' % len(all_citing_author_paper_tuple_list),
                                     total=len(all_citing_author_paper_tuple_list)):
            author_paper_affiliation_tuple_list.append(__affiliations_from_authors(author_and_paper))

    # Filter empty items.
    author_paper_affiliation_tuple_list = [item for item in author_paper_affiliation_tuple_list if item]
    return author_paper_affiliation_tuple_list

def clean_affiliation_names(author_paper_affiliation_tuple_list: List[Tuple[str]]) -> List[Tuple[str]]:
    '''
    Optional Step. Clean up the names of affiliations from the authors' affiliation tab on their Google Scholar profiles.
    
    Now handles tuples with venue: (author_name, citing_paper_title, cited_paper_title, venue, affiliation)
    '''
    cleaned_author_paper_affiliation_tuple_list = []
    for author_name, citing_paper_title, cited_paper_title, venue, affiliation_string in author_paper_affiliation_tuple_list:
        if author_name == NO_AUTHOR_FOUND_STR:
            cleaned_author_paper_affiliation_tuple_list.append((NO_AUTHOR_FOUND_STR, citing_paper_title, cited_paper_title, venue, NO_AUTHOR_FOUND_STR))
        else:
            # Use a regular expression to split the string by ';' or 'and'.
            substring_list = [part.strip() for part in re.split(r'[;]|\band\b', affiliation_string)]
            # Further split the substrings by ',' if the latter component is not a country.
            substring_list = __country_aware_comma_split(substring_list)

            for substring in substring_list:
                # Use a regular expression to remove anything before 'at', or '@'.
                cleaned_affiliation = re.sub(r'.*?\bat\b|.*?@', '', substring, flags=re.IGNORECASE).strip()
                # Use a regular expression to filter out strings that represent
                # a person's identity rather than affiliation.
                is_common_identity_string = re.search(
                    re.compile(
                        r'\b(director|manager|chair|engineer|programmer|scientist|professor|lecturer|phd|ph\.d|postdoc|doctor|student|department of)\b',
                        re.IGNORECASE),
                    cleaned_affiliation)
                if not is_common_identity_string:
                    cleaned_author_paper_affiliation_tuple_list.append((author_name, citing_paper_title, cited_paper_title, venue, cleaned_affiliation))
    return cleaned_author_paper_affiliation_tuple_list

def fill_known_affiliations(affiliation_name: str) -> Optional[str]:
    '''
    If the affiliation is known, return its geolocation.
    If not, return None.
    '''
    for key in KNOWN_AFFILIATION_DICT:
        if key in affiliation_name.lower():
            return KNOWN_AFFILIATION_DICT[key]
    return None

def affiliation_invalid(affiliation_name: str) -> bool:
    '''
    Check if the affiliation is invalid.
    '''
    invalid_affiliation_set = {
        NO_AUTHOR_FOUND_STR.lower(),
        'computer', 'computer science', 'electrical', 'engineering', 'researcher',
        'scholar', 'inc.', 'school', 'department', 'student', 'candidate', 'professor', 'faculty', 'associate'
    }
    for key in invalid_affiliation_set:
        if key in affiliation_name.lower():
            return True
    return False

def affiliation_text_to_geocode(author_paper_affiliation_tuple_list: List[Tuple[str]], max_attempts: int = 3) -> List[Tuple[str]]:
    '''
    Step 4: Convert affiliations in plain text to Geocode.
    
    Now handles tuples with venue: (author_name, citing_paper_title, cited_paper_title, venue, affiliation)
    Returns: (author_name, citing_paper_title, cited_paper_title, venue, affiliation, lat, lon, county, city, state, country)
    '''
    coordinates_and_info = []
    geolocator = Nominatim(user_agent='citation_mapper')

    # Find unique affiliations and record their corresponding entries.
    affiliation_map = {}
    for entry_idx, entry in enumerate(author_paper_affiliation_tuple_list):
        # Handle both old format (4 elements) and new format (5 elements with venue)
        if len(entry) == 5:
            _, _, _, _, affiliation_name = entry
        else:
            _, _, _, affiliation_name = entry
            
        if affiliation_name not in affiliation_map.keys():
            affiliation_map[affiliation_name] = [entry_idx]
        else:
            affiliation_map[affiliation_name].append(entry_idx)

    num_total_affiliations = len(affiliation_map)
    num_located_affiliations = 0
    for affiliation_name in tqdm(affiliation_map,
                                 desc='Finding geographic coordinates from %d unique citing affiliations in %d entries' % (
                                     len(affiliation_map), len(author_paper_affiliation_tuple_list)),
                                 total=len(affiliation_map)):
        if affiliation_invalid(affiliation_name):
            corresponding_entries = affiliation_map[affiliation_name]
            for entry_idx in corresponding_entries:
                entry = author_paper_affiliation_tuple_list[entry_idx]
                if len(entry) == 5:
                    author_name, citing_paper_title, cited_paper_title, venue, affiliation_name = entry
                else:
                    author_name, citing_paper_title, cited_paper_title, affiliation_name = entry
                    venue = NO_VENUE_FOUND_STR
                coordinates_and_info.append((author_name, citing_paper_title, cited_paper_title, venue, affiliation_name,
                                            '', '', '', '', '', ''))
        else:
            geo_location = fill_known_affiliations(affiliation_name)
            if geo_location is not None:
                county, city, state, country, latitude, longitude = geo_location
                corresponding_entries = affiliation_map[affiliation_name]
                for entry_idx in corresponding_entries:
                    entry = author_paper_affiliation_tuple_list[entry_idx]
                    if len(entry) == 5:
                        author_name, citing_paper_title, cited_paper_title, venue, affiliation_name = entry
                    else:
                        author_name, citing_paper_title, cited_paper_title, affiliation_name = entry
                        venue = NO_VENUE_FOUND_STR
                    coordinates_and_info.append((author_name, citing_paper_title, cited_paper_title, venue, affiliation_name,
                                                latitude, longitude, county, city, state, country))
                num_located_affiliations += 1
            else:
                for _ in range(max_attempts):
                    try:
                        geo_location = geolocator.geocode(affiliation_name)
                        if geo_location is not None:
                            location_metadata = geolocator.reverse(str(geo_location.latitude) + ',' + str(geo_location.longitude), language='en')
                            address = location_metadata.raw['address']
                            county, city, state, country = None, None, None, None
                            if 'county' in address:
                                county = address['county']
                            if 'city' in address:
                                city = address['city']
                            if 'state' in address:
                                state = address['state']
                            if 'country' in address:
                                country = address['country']

                            corresponding_entries = affiliation_map[affiliation_name]
                            for entry_idx in corresponding_entries:
                                entry = author_paper_affiliation_tuple_list[entry_idx]
                                if len(entry) == 5:
                                    author_name, citing_paper_title, cited_paper_title, venue, affiliation_name = entry
                                else:
                                    author_name, citing_paper_title, cited_paper_title, affiliation_name = entry
                                    venue = NO_VENUE_FOUND_STR
                                coordinates_and_info.append((author_name, citing_paper_title, cited_paper_title, venue, affiliation_name,
                                                            geo_location.latitude, geo_location.longitude,
                                                            county, city, state, country))
                            num_located_affiliations += 1
                            break
                    except:
                        continue
    print('\nConverted %d/%d affiliations to Geocodes.' % (num_located_affiliations, num_total_affiliations))
    coordinates_and_info = [item for item in coordinates_and_info if item is not None]
    return coordinates_and_info

def export_dict_to_csv(coordinates_and_info: List[Tuple[str]], csv_output_path: str) -> None:
    '''
    Step 5.1: Export csv file recording citation information.
    Now includes venue/journal column.
    '''
    citation_df = pd.DataFrame(coordinates_and_info,
                               columns=['citing author name', 'citing paper title', 'cited paper title',
                                        'journal/venue', 'affiliation', 'latitude', 'longitude',
                                        'county', 'city', 'state', 'country'])

    citation_df.to_csv(csv_output_path)
    return


def export_journals_csv(coordinates_and_info: List[Tuple[str]], journals_csv_path: str) -> None:
    '''
    Export a separate CSV file with just unique journals/venues and their citation counts.
    '''
    # Extract venue information
    venue_counts = {}
    for entry in coordinates_and_info:
        if len(entry) >= 4:
            venue = entry[3]  # venue is at index 3
            if venue and venue != NO_VENUE_FOUND_STR:
                venue_counts[venue] = venue_counts.get(venue, 0) + 1
    
    # Create DataFrame sorted by citation count
    journals_df = pd.DataFrame([
        {'journal/venue': venue, 'citation_count': count}
        for venue, count in venue_counts.items()
    ])
    
    if not journals_df.empty:
        journals_df = journals_df.sort_values('citation_count', ascending=False)
    
    journals_df.to_csv(journals_csv_path, index=False)
    print(f'\nJournals/venues exported to {journals_csv_path}')
    print(f'Found {len(journals_df)} unique journals/venues.')
    return


def read_csv_to_dict(csv_path: str) -> None:
    '''
    Step 5.1: Read csv file recording citation information.
    '''
    citation_df = pd.read_csv(csv_path, index_col=0)
    coordinates_and_info = list(citation_df.itertuples(index=False, name=None))
    return coordinates_and_info

def create_map(coordinates_and_info: List[Tuple[str]], pin_colorful: bool = True):
    '''
    Step 5.2: Create the Citation World Map.
    Updated to handle the new tuple format with venue.
    '''
    citation_map = folium.Map(location=[20, 0], zoom_start=2)

    # Find unique affiliations and record their corresponding entries.
    # Affiliation is now at index 4 (was index 3)
    affiliation_map = {}
    for entry_idx, entry in enumerate(coordinates_and_info):
        affiliation_name = entry[4] if len(entry) > 10 else entry[3]  # Handle both formats
        if affiliation_name == NO_AUTHOR_FOUND_STR:
            continue
        elif affiliation_name not in affiliation_map.keys():
            affiliation_map[affiliation_name] = [entry_idx]
        else:
            affiliation_map[affiliation_name].append(entry_idx)

    if pin_colorful:
        colors = ['red', 'blue', 'green', 'purple', 'orange', 'darkred',
                  'lightred', 'beige', 'darkblue', 'darkgreen', 'cadetblue',
                  'darkpurple', 'pink', 'lightblue', 'lightgreen',
                  'gray', 'black', 'lightgray']
        for affiliation_name in affiliation_map:
            color = random.choice(colors)
            corresponding_entries = affiliation_map[affiliation_name]
            author_name_list = []
            location_valid = True
            for entry_idx in corresponding_entries:
                entry = coordinates_and_info[entry_idx]
                author_name = entry[0]
                # Latitude/longitude indices depend on format
                if len(entry) > 10:  # New format with venue
                    lat, lon = entry[5], entry[6]
                else:  # Old format
                    lat, lon = entry[4], entry[5]
                if pd.isna(lat) or pd.isna(lon) or lat == '' or lon == '':
                    location_valid = False
                author_name_list.append(author_name)
            if location_valid:
                folium.Marker([lat, lon], popup='%s (%s)' % (affiliation_name, ' & '.join(author_name_list)),
                            icon=folium.Icon(color=color)).add_to(citation_map)
    else:
        for affiliation_name in affiliation_map:
            corresponding_entries = affiliation_map[affiliation_name]
            author_name_list = []
            location_valid = True
            for entry_idx in corresponding_entries:
                entry = coordinates_and_info[entry_idx]
                author_name = entry[0]
                if len(entry) > 10:
                    lat, lon = entry[5], entry[6]
                else:
                    lat, lon = entry[4], entry[5]
                if pd.isna(lat) or pd.isna(lon) or lat == '' or lon == '':
                    location_valid = False
                author_name_list.append(author_name)
            if location_valid:
                folium.Marker([lat, lon], popup='%s (%s)' % (affiliation_name, ' & '.join(author_name_list))).add_to(citation_map)
    return citation_map

def count_citation_stats(coordinates_and_info: List[Tuple[str]]) -> List[int]:
    '''
    Count the number of citing authors, affiliations, countries, and journals.
    '''
    unique_author_list, unique_affiliation_list, unique_country_list, unique_journal_list = set(), set(), set(), set()
    for entry in coordinates_and_info:
        if len(entry) > 10:  # New format
            author_name, _, _, venue, affiliation_name = entry[0], entry[1], entry[2], entry[3], entry[4]
            country = entry[10]
        else:  # Old format
            author_name, _, _, affiliation_name = entry[0], entry[1], entry[2], entry[3]
            country = entry[9]
            venue = ''
            
        if affiliation_name == NO_AUTHOR_FOUND_STR:
            continue
        unique_author_list.add(author_name)
        unique_affiliation_list.add(affiliation_name)
        unique_country_list.add(country)
        if venue and venue != NO_VENUE_FOUND_STR:
            unique_journal_list.add(venue)
            
    num_authors = len(unique_author_list)
    num_affiliations = len(unique_affiliation_list)
    num_countries = len(unique_country_list)
    num_journals = len(unique_journal_list)
    return num_authors, num_affiliations, num_countries, num_journals

def __fill_publication_metadata(pub):
    time.sleep(random.uniform(1, 5))
    return scholarly.fill(pub)

def __citing_authors_and_papers_from_publication(cites_id_and_cited_paper: Tuple[str, str]):
    '''
    Updated to include venue information from get_citing_author_ids_and_citing_papers.
    '''
    cites_id, cited_paper_title = cites_id_and_cited_paper
    citing_paper_search_url = 'https://scholar.google.com/scholar?hl=en&cites=' + cites_id
    citing_authors_papers_venues = get_citing_author_ids_and_citing_papers(citing_paper_search_url)
    citing_author_paper_info = []
    for citing_author_id, citing_paper_title, venue in citing_authors_papers_venues:
        citing_author_paper_info.append((citing_author_id, citing_paper_title, cited_paper_title, venue))
    return citing_author_paper_info

def __affiliations_from_authors_conservative(citing_author_paper_info: str):
    '''
    Conservative: only use Google Scholar verified organization.
    Updated to handle venue in tuple.
    '''
    if len(citing_author_paper_info) == 4:
        citing_author_id, citing_paper_title, cited_paper_title, venue = citing_author_paper_info
    else:
        citing_author_id, citing_paper_title, cited_paper_title = citing_author_paper_info
        venue = NO_VENUE_FOUND_STR
        
    if citing_author_id == NO_AUTHOR_FOUND_STR:
        return (NO_AUTHOR_FOUND_STR, citing_paper_title, cited_paper_title, venue, NO_AUTHOR_FOUND_STR)

    time.sleep(random.uniform(1, 5))
    citing_author = scholarly.search_author_id(citing_author_id)

    if 'organization' in citing_author:
        try:
            author_organization = get_organization_name(citing_author['organization'])
            return (citing_author['name'], citing_paper_title, cited_paper_title, venue, author_organization)
        except Exception as e:
            print('[Warning!]', e)
            return None
    return None

def __affiliations_from_authors_aggressive(citing_author_paper_info: str):
    '''
    Aggressive: use the self-reported affiliation string.
    Updated to handle venue in tuple.
    '''
    if len(citing_author_paper_info) == 4:
        citing_author_id, citing_paper_title, cited_paper_title, venue = citing_author_paper_info
    else:
        citing_author_id, citing_paper_title, cited_paper_title = citing_author_paper_info
        venue = NO_VENUE_FOUND_STR
        
    if citing_author_id == NO_AUTHOR_FOUND_STR:
        return (NO_AUTHOR_FOUND_STR, citing_paper_title, cited_paper_title, venue, NO_AUTHOR_FOUND_STR)

    time.sleep(random.uniform(1, 5))
    citing_author = scholarly.search_author_id(citing_author_id)
    if 'affiliation' in citing_author:
        return (citing_author['name'], citing_paper_title, cited_paper_title, venue, citing_author['affiliation'])
    return None

def __country_aware_comma_split(string_list: List[str]) -> List[str]:
    comma_split_list = []

    for part in string_list:
        sub_parts = [sub_part.strip() for sub_part in re.split(r'[,，]', part)]
        sub_parts_iter = iter(sub_parts)

        for sub_part in sub_parts_iter:
            if __iscountry(sub_part):
                continue
            next_part = next(sub_parts_iter, None)
            if __iscountry(next_part):
                comma_split_list.append(f"{sub_part}, {next_part}")
            else:
                comma_split_list.append(sub_part)
                if next_part:
                    comma_split_list.append(next_part)
    return comma_split_list

def __iscountry(string: str) -> bool:
    try:
        pycountry.countries.lookup(string)
        return True
    except LookupError:
        return False

def __print_author_and_affiliation(author_paper_affiliation_tuple_list: List[Tuple[str]]) -> None:
    __author_affiliation_tuple_list = []
    for entry in sorted(author_paper_affiliation_tuple_list):
        author_name = entry[0]
        affiliation_name = entry[-1]  # Affiliation is always last
        if author_name == NO_AUTHOR_FOUND_STR:
            continue
        __author_affiliation_tuple_list.append((author_name, affiliation_name))

    __author_affiliation_tuple_list = list(set(__author_affiliation_tuple_list))
    for author_name, affiliation_name in sorted(__author_affiliation_tuple_list):
        print('Author: %s. Affiliation: %s.' % (author_name, affiliation_name))
    print('')
    return


def save_cache(data: Any, fpath: str) -> None:
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, "wb") as fd:
        pickle.dump(data, fd)

def load_cache(fpath: str) -> Any:
    with open(fpath, "rb") as fd:
        return pickle.load(fd)

def generate_citation_map(scholar_id: str,
                          output_path: str = 'citation_map.html',
                          csv_output_path: str = 'citation_info.csv',
                          journals_csv_path: str = 'journals_info.csv',
                          parse_csv: bool = False,
                          cache_folder: str = 'cache',
                          affiliation_conservative: bool = False,
                          num_processes: int = 16,
                          use_proxy: bool = False,
                          pin_colorful: bool = True,
                          print_citing_affiliations: bool = True):
    '''
    Google Scholar Citation World Map.

    Parameters
    ----
    scholar_id: str
        Your Google Scholar ID.
    output_path: str
        (default is 'citation_map.html')
        The path to the output HTML file.
    csv_output_path: str
        (default is 'citation_info.csv')
        The path to the output csv file.
    journals_csv_path: str
        (default is 'journals_info.csv')
        The path to the output csv file with journal/venue summary.
    parse_csv: bool
        (default is False)
        If True, will directly jump to Step 5.2, using the information loaded from the csv.
    cache_folder: str
        (default is 'cache')
        The folder to save intermediate results.
    affiliation_conservative: bool
        (default is False)
        If true, we will use a more conservative approach to identify affiliations.
    num_processes: int
        (default is 16)
        Number of processes for parallel processing.
    use_proxy: bool
        (default is False)
        If true, we will use a scholarly proxy.
    pin_colorful: bool
        (default is True)
        If true, the location pins will have a variety of colors.
    print_citing_affiliations: bool
        (default is True)
        If true, print the list of citing affiliations.
    '''

    if not parse_csv:

        if use_proxy:
            pg = ProxyGenerator()
            pg.FreeProxies()
            scholarly.use_proxy(pg)
            print('Using proxy.')

        if cache_folder is not None:
            cache_path = os.path.join(cache_folder, scholar_id, 'all_citing_author_paper_tuple_list.pkl')
        else:
            cache_path = None

        if cache_path is None or not os.path.exists(cache_path):
            print('No cache found for this author. Finding citing authors from scratch.\n')

            all_citing_author_paper_tuple_list = find_all_citing_authors(scholar_id=scholar_id,
                                                                         num_processes=num_processes)
            print('A total of %d citing authors recorded.\n' % len(all_citing_author_paper_tuple_list))
            if cache_path is not None and len(all_citing_author_paper_tuple_list) > 0:
                save_cache(all_citing_author_paper_tuple_list, cache_path)
            print('Saved to cache: %s.\n' % cache_path)

        else:
            print('Cache found. Loading author paper information from cache.\n')
            all_citing_author_paper_tuple_list = load_cache(cache_path)
            print('Loaded from cache: %s.\n' % cache_path)
            print('A total of %d citing authors loaded.\n' % len(all_citing_author_paper_tuple_list))

        if cache_folder is not None:
            cache_path = os.path.join(cache_folder, scholar_id, 'author_paper_affiliation_tuple_list.pkl')
        else:
            cache_path = None

        if cache_path is None or not os.path.exists(cache_path):
            print('No cache found for this author. Finding citing affiliations from scratch.\n')

            print('Identifying affiliations using the %s approach.' % ('conservative' if affiliation_conservative else 'aggressive'))
            author_paper_affiliation_tuple_list = find_all_citing_affiliations(all_citing_author_paper_tuple_list,
                                                                            num_processes=num_processes,
                                                                            affiliation_conservative=affiliation_conservative)
            print('\nA total of %d citing affiliations recorded.\n' % len(author_paper_affiliation_tuple_list))
            author_paper_affiliation_tuple_list = list(set(author_paper_affiliation_tuple_list))

            if print_citing_affiliations:
                if affiliation_conservative:
                    print('Taking the conservative approach. Will not need to clean the affiliation names.')
                    print('List of all citing authors and affiliations:\n')
                else:
                    print('Taking the aggressive approach. Cleaning the affiliation names.')
                    print('List of all citing authors and affiliations before cleaning:\n')
                __print_author_and_affiliation(author_paper_affiliation_tuple_list)
            if not affiliation_conservative:
                cleaned_author_paper_affiliation_tuple_list = clean_affiliation_names(author_paper_affiliation_tuple_list)
                if print_citing_affiliations:
                    print('List of all citing authors and affiliations after cleaning:\n')
                    __print_author_and_affiliation(cleaned_author_paper_affiliation_tuple_list)
                author_paper_affiliation_tuple_list += cleaned_author_paper_affiliation_tuple_list
                author_paper_affiliation_tuple_list = list(set(author_paper_affiliation_tuple_list))

            if cache_path is not None and len(author_paper_affiliation_tuple_list) > 0:
                save_cache(author_paper_affiliation_tuple_list, cache_path)
            print('Saved to cache: %s.\n' % cache_path)

        else:
            print('Cache found. Loading author paper and affiliation information from cache.\n')
            author_paper_affiliation_tuple_list = load_cache(cache_path)
            print('List of all citing authors and affiliations loaded:\n')
            __print_author_and_affiliation(author_paper_affiliation_tuple_list)

        coordinates_and_info = affiliation_text_to_geocode(author_paper_affiliation_tuple_list)
        coordinates_and_info = sorted(list(set(coordinates_and_info)))

        export_dict_to_csv(coordinates_and_info, csv_output_path)
        print('\nCitation information exported to %s.' % csv_output_path)
        
        # Export journals summary CSV
        export_journals_csv(coordinates_and_info, journals_csv_path)

    else:
        print('\nDirectly parsing the csv. Skipping all previous steps.')
        assert os.path.isfile(csv_output_path), '`csv_output_path` is not a file.'
        coordinates_and_info = read_csv_to_dict(csv_output_path)
        print('\nCitation information loaded from %s.' % csv_output_path)

    citation_map = create_map(coordinates_and_info, pin_colorful=pin_colorful)
    citation_map.save(output_path)
    print('\nHTML map created and saved at %s.\n' % output_path)

    stats = count_citation_stats(coordinates_and_info)
    if len(stats) == 4:
        num_authors, num_affiliations, num_countries, num_journals = stats
        print('\nYou have been cited by %s researchers from %s affiliations and %s countries.' % (
            num_authors, num_affiliations, num_countries))
        print('Citations appeared in %s unique journals/venues.\n' % num_journals)
    else:
        num_authors, num_affiliations, num_countries = stats
        print('\nYou have been cited by %s researchers from %s affiliations and %s countries.\n' % (
            num_authors, num_affiliations, num_countries))
    return


if __name__ == '__main__':
    scholar_id = '3rDjnykAAAAJ'
    generate_citation_map(scholar_id,
                          output_path='citation_map.html',
                          csv_output_path='citation_info.csv',
                          journals_csv_path='journals_info.csv',
                          parse_csv=False,
                          cache_folder='cache',
                          affiliation_conservative=True,
                          num_processes=16,
                          use_proxy=False,
                          pin_colorful=True,
                          print_citing_affiliations=True)
