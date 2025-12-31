# Copyright (c) 2024 Chen Liu
# Modified to include journal/venue extraction
# All rights reserved.
import random
import re
import time
from bs4 import BeautifulSoup
from typing import List, Tuple
from selenium import webdriver

NO_AUTHOR_FOUND_STR = 'No_author_found'
NO_VENUE_FOUND_STR = ''

# Observation: the Nominatim package is very bad at getting the geolocation of companies (geolocation of universities are fine).
# Temporary solution: hard code the geolocations of the companies.
# NOTE: The headquarter represents the whole company which usually has many offices accross the world.
KNOWN_AFFILIATION_DICT = {
    'amazon': ('King County', 'Seattle', 'Washington', 'USA', 47.622721, -122.337176),
    'meta': ('Menlo Park', 'San Mateo', 'California', 'USA', 37.4851, -122.1483),
    'microsoft': ('King County', 'Redmond', 'Washington', 'USA', 47.645695, -122.131803),
    'ibm': ('Westchester', 'Armonk', 'New York', 'USA', 41.108252, -73.719887),
    'google': ('Santa Clara', 'Mountain View', 'California', 'USA', 37.421473, -122.080679),
    'morgan stanley': ('New York', 'New York', 'New York', 'USA', 40.760251, -73.98518),
    'siemens healthineers': ('Forchheim', 'Forchheim', 'Bavaria', 'Germany', 49.702088, 11.055870),
    'oracle': ('Travis', 'Austin', 'Texas', 'USA', 30.242913, -97.721641)
}

global_driver = None

def get_driver():
    global global_driver
    if global_driver is None:
        global_driver = webdriver.Chrome()
        print("[INFO] Browser opened. You can solve CAPTCHAs (if prompted) in the browser window.")
        print("[INFO] KEEP THE POP-UP BROWSER OPEN until the CitationMap program is complete.")
    return global_driver

def wait_for_captcha(driver):
    '''
    Wait for user to solve CAPTCHA if present.
    '''
    page_source = driver.page_source
    if 'CAPTCHA' in page_source or 'not a robot' in page_source:
        print("\n" + "="*60)
        print("CAPTCHA DETECTED! Please solve it in the browser.")
        print("Press Enter here after you've solved it...")
        print("="*60)
        input()  # Wait for user to press Enter
        time.sleep(1)
    return


def is_invalid_venue(text: str) -> bool:
    '''
    Check if the text is clearly NOT a venue (just a year, empty, too short).
    This is intentionally minimal - we want to capture what Google Scholar shows,
    even if it's a publisher name rather than a journal name.
    '''
    text_lower = text.lower().strip()
    
    # Empty or too short
    if len(text_lower) < 3:
        return True
    
    # Just a year
    if re.match(r'^\d{4}$', text_lower):
        return True
    
    # Ellipsis only
    if text_lower in ['…', '...']:
        return True
    
    return False


def extract_venue_from_gs_a(gs_a_text: str) -> str:
    '''
    Extract venue/journal/publisher name from the Google Scholar author line (gs_a).
    
    The gs_a line format varies but is typically:
    "Author1, Author2 - Journal/Venue, Year - Publisher/Domain"
    or
    "Author1, Author2 - Publisher, Year"
    or
    "Author1, Author2 - Year - Publisher"
    
    Google Scholar is inconsistent - sometimes showing journal names, sometimes publishers.
    This function captures whatever is in the "venue" position.
    
    Returns the venue/journal/publisher name or empty string if not found.
    '''
    if not gs_a_text:
        return NO_VENUE_FOUND_STR
    
    # Split by " - " to separate components
    parts = gs_a_text.split(' - ')
    
    if len(parts) < 2:
        return NO_VENUE_FOUND_STR
    
    # Strategy: Look at parts after the authors (first part)
    # Try to find the most likely venue/journal
    
    # Case 1: Three or more parts: "Authors - Venue, Year - Publisher"
    # Try the middle part first (more likely to be journal), fall back to last part
    if len(parts) >= 3:
        middle_part = parts[1]
        last_part = parts[-1]
        
        # Check if middle part starts with a year (means no venue in middle)
        if re.match(r'^\d{4}', middle_part.strip()):
            # No venue in middle, try last part
            venue = last_part.strip().strip('…').strip()
            if not is_invalid_venue(venue):
                return venue
        else:
            # Middle part has venue info - extract it (remove year if present)
            venue = re.sub(r',?\s*\d{4}.*$', '', middle_part).strip().strip('…').strip()
            if not is_invalid_venue(venue):
                return venue
            # If middle didn't work, try last part
            venue = last_part.strip().strip('…').strip()
            if not is_invalid_venue(venue):
                return venue
    
    # Case 2: Two parts: "Authors - Venue, Year" or "Authors - Year"
    elif len(parts) == 2:
        second_part = parts[1]
        
        # Check if this part starts with a year (no venue)
        if re.match(r'^\d{4}', second_part.strip()):
            return NO_VENUE_FOUND_STR
        
        # Remove year and everything after
        venue = re.sub(r',?\s*\d{4}.*$', '', second_part).strip().strip('…').strip()
        
        if not is_invalid_venue(venue):
            return venue
    
    return NO_VENUE_FOUND_STR


def get_html_per_citation_page(soup) -> List[Tuple[str, str, str]]:
    '''
    Utility to query each page containing results for cited work.
    
    Parameters
    --------
    soup: Beautiful Soup object pointing to current page.
    
    Returns
    --------
    List of tuples: (author_id, paper_title, venue/journal)
    '''
    citing_authors_papers_venues = []

    for result in soup.find_all('div', class_='gs_ri'):
        title_tag = result.find('h3', class_='gs_rt')
        if title_tag:
            paper_parsed = False
            author_links = result.find_all('a', href=True)
            title_text = title_tag.get_text()
            title = title_text.replace('[HTML]', '').replace('[PDF]', '').strip()
            
           # Extract venue/journal from the gs_a line (author/venue/year line)
            gs_a_tag = result.find('div', class_='gs_a')
            venue = NO_VENUE_FOUND_STR
            if gs_a_tag:
                gs_a_text = gs_a_tag.get_text()
                print(f"[DEBUG] gs_a raw text: {repr(gs_a_text)}")
                venue = extract_venue_from_gs_a(gs_a_text)
                print(f"[DEBUG] extracted venue: {repr(venue)}")
            
            for link in author_links:
                if 'user=' in link['href']:
                    author_id = link['href'].split('user=')[1].split('&')[0]
                    citing_authors_papers_venues.append((author_id, title, venue))
                    paper_parsed = True
            if not paper_parsed:
                print("[WARNING!] Could not find author links for ", title)
                citing_authors_papers_venues.append((NO_AUTHOR_FOUND_STR, title, venue))
        else:
            continue
    return citing_authors_papers_venues


def get_citing_author_ids_and_citing_papers(paper_url: str) -> List[Tuple[str, str, str]]:
    '''
    Find the (Google Scholar IDs of authors, titles of papers, venues) who cite a given paper on Google Scholar.

    Parameters
    --------
    paper_url: URL of the paper BEING cited.
    
    Returns
    --------
    List of tuples: (author_id, paper_title, venue/journal)
    '''
    citing_authors_papers_venues = []

    driver = get_driver()
    time.sleep(random.uniform(1, 5))  # Random delay to reduce risk of being blocked.

    # Search the url of all citing papers.
    driver.get(paper_url)
    wait_for_captcha(driver)

    # Get the HTML data.
    soup = BeautifulSoup(driver.page_source, 'html.parser')

    # Check for common indicators of blocking
    if 'Access Denied' in soup.text or 'Forbidden' in soup.text:
        print('[WARNING!] Access denied or forbidden when searching searching %s.' % paper_url)
        return []

    # Loop through the citation results and find citing authors and papers.
    current_page_number = 1
    citing_authors_papers_venues += get_html_per_citation_page(soup)

    # Find the page navigation.
    navigation_buttons = soup.find_all('a', class_='gs_nma')
    for navigation in navigation_buttons:
        page_number_str = navigation.text
        if page_number_str and page_number_str.isnumeric() and int(page_number_str) == current_page_number + 1:
            # Found the correct button for next page.
            current_page_number += 1
            next_url = 'https://scholar.google.com' + navigation['href']
            time.sleep(random.uniform(1, 5))  # Random delay to reduce risk of being blocked.

            driver.get(next_url)
            wait_for_captcha(driver)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            citing_authors_papers_venues += get_html_per_citation_page(soup)
        else:
            continue

    return citing_authors_papers_venues

def get_organization_name(organization_id: str) -> str:
    '''
    Get the official name of the organization defined by the unique Google Scholar organization ID.
    '''

    driver = get_driver()
    time.sleep(random.uniform(1, 5))  # Random delay to reduce risk of being blocked.

    url = f'https://scholar.google.com/citations?view_op=view_org&org={organization_id}&hl=en'

    time.sleep(random.uniform(1, 5))  # Random delay to reduce risk of being blocked.

    driver.get(url)
    wait_for_captcha(driver)

    soup = BeautifulSoup(driver.page_source, 'html.parser')
    tag = soup.find('h2', {'class': 'gsc_authors_header'})
    if not tag:
        raise Exception(f'When getting organization name, failed to parse {url}.')
    return tag.text.replace('Learn more', '').strip()
