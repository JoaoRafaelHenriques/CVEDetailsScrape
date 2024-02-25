#!/usr/bin/env python3

"""
	This script inserts the data from any CSV files generated by "create_file_timeline.py" into the PATCHES table in the database.
	Before running this script, the previously collected vulnerability data must be inserted into the VULNERABILITIES table using
	"insert_vulnerabilities_in_database.py".
"""

from typing import cast

import numpy as np # type: ignore
import pandas as pd # type: ignore
import sys

from modules.common import log, deserialize_json_container
from modules.database import Database
from modules.project import Project

from emails.send_email import Email

####################################################################################################

INPUT = "affected-files.csv"
OUTPUT = "file-timeline.csv"

####################################################################################################

def send_notification(proj: str):
    '''
	Sends a notification to email.

	Params:
		proj(str): name of the project 
    '''
    email = Email()
    email.start()
    email.send("[AUTO] COLLECT_VULNERABILITIES", f"The insertion of new PATCHES for the project {proj} is over.")

def get_next_patches_table_id(next_id, id_template):
	""" Retrieves the next primary key value of the P_ID column for the current project. """
	result = id_template.replace('<ID>', str(next_id))
	next_id += 1
	return result, next_id
   
def main(project_to_analizys: str):
	'''
		Function thats starts the process.
		The collection will start for the project of the params.
  
		Params:
			project_to_analizys(str): name of the project or an empty string that represents everthing except kernel and mozilla 
 	'''
	with Database(buffered=True) as db:

		# Get the information of the projects
		project_list = Project.get_project_list_from_config()
  
		# Iterate for each one and continue only if the the params correspond
		for project in project_list:
			if project.short_name == project_to_analizys:
				pass
			elif project_to_analizys == "":
				if project.short_name != 'mozilla' and project.short_name != 'kernel':
					pass
				else:
					log.info(f'The project {project.short_name} will be skiped.')
					continue
			else:
				log.info(f'The project {project.short_name} will be skiped.')
				continue	
			
			success, error_code = db.execute_query(	'''
													SELECT
														(SELECT MAX(REGEXP_SUBSTR(P_ID, '[0-9]+') + 0) + 1 FROM PATCHES WHERE R_ID = %(R_ID)s) AS NEXT_ID,
														(SELECT REGEXP_REPLACE(P_ID, '[0-9]+', '<ID>') FROM PATCHES WHERE R_ID = %(R_ID)s LIMIT 1) AS ID_TEMPLATE;
													''',
													params={'R_ID': project.database_id})

			assert db.cursor.rowcount != -1, 'The database cursor must be buffered.'

			next_id = -1
			id_template = ''
			
			if success and db.cursor.rowcount > 0:
				row = db.cursor.fetchone()
				
				if row['NEXT_ID'] is None:
					row['ID_TEMPLATE'] = project.database_name + "<ID>p"
					row['NEXT_ID'] = 1
				
				next_id = int(row['NEXT_ID'])
				id_template = row['ID_TEMPLATE']
				log.info(f'Found the next commit ID {next_id} with the template "{id_template}" for the project "{project}".')

			else:
				log.error(f'Failed to find the next commit ID for the project "{project}" with the error code {error_code}.')
				continue

			input_csv_path, output = project.find_last_diff_cves(project.output_directory_diff_path, project, INPUT, OUTPUT)

			if output == None:
				continue

			log.info(f'Inserting the patches for the project "{project}" using the information in "{input_csv_path}".')

			# We only want the neutral commits since the vulnerable ones are identified relative to neutral commits
			# by using the Occurrence column in the metrics table (vulnerable = before neutral).
			commits = pd.read_csv(input_csv_path, usecols=['Topological Index', 'Neutral Commit Hash', 'Neutral Tag Name', 'Neutral Author Date', 'CVEs'], dtype=str)
			commits.drop_duplicates(subset=['Topological Index'], inplace=True)
			commits = commits.replace({np.nan: None})
   
			for _, row in commits.iterrows():

				topological_index = row['Topological Index']
				commit_hash = row['Neutral Commit Hash']

				success, error_code = db.execute_query('SELECT * FROM PATCHES WHERE P_COMMIT = %(P_COMMIT)s LIMIT 1;', params={'P_COMMIT': commit_hash})

				if db.cursor.rowcount > 0:
					log.info(f'Skipping the commit {commit_hash} ({topological_index}) for the project "{project}" since it already exists.')
					continue

				commit_tag_name = row['Neutral Tag Name']
				commit_author_date = row['Neutral Author Date']
				cve_list = cast(list, deserialize_json_container(row['CVEs'], [None]))

				for cve in cve_list:

					success, error_code = db.execute_query('SELECT V_ID FROM VULNERABILITIES WHERE CVE = %(CVE)s;', params={'CVE': cve})

					if success:

						if db.cursor.rowcount == 0:
							log.error(f'Could not find any vulnerability ID for {cve} when attempting to insert the commit {commit_hash} ({topological_index}, {commit_tag_name}, {commit_author_date}) for the project "{project}".')

						for database_row in db.cursor:

							v_id = database_row['V_ID']
							p_id, next_id = get_next_patches_table_id(next_id, id_template)

							success, error_code = db.execute_query(	'''
																	INSERT INTO PATCHES
																	(
																		P_ID, P_URL, V_ID, R_ID, P_COMMIT,
																		ERROR_SIMILARITY, SITUATION, RELEASES, DATE, Observations
																	)
																	VALUES
																	(
																		%(P_ID)s, %(P_URL)s, %(V_ID)s, %(R_ID)s, %(P_COMMIT)s,
																		%(ERROR_SIMILARITY)s, %(SITUATION)s, %(RELEASES)s, %(DATE)s, %(Observations)s
																	);
																	''',
																	
																	params={
																		'P_ID': p_id,
																		'P_URL': 'TBD',
																		'V_ID': v_id,
																		'R_ID': project.database_id,
																		'P_COMMIT': commit_hash,
																		'ERROR_SIMILARITY': 'TBD',
																		'SITUATION': -1,
																		'RELEASES': commit_tag_name,
																		'DATE': commit_author_date,
																		'Observations': 'TBD',
																	}
																)

							if success:
								log.info(f'Inserted the commit {commit_hash} ({topological_index}, {commit_tag_name}, {commit_author_date}, {cve}, {v_id}) for the project "{project}".')
							else:
								log.error(f'Failed to insert the commit {commit_hash} ({topological_index}, {commit_tag_name}, {commit_author_date}, {cve}, {v_id}) for the project "{project}" with the error code {error_code}.')

					else:
						log.error(f'Could not list the existing vulnerability IDs for {cve} when attempting to insert the commit {commit_hash} ({topological_index}, {commit_tag_name}, {commit_author_date}) for the project "{project}" with the error code {error_code} ({db.cursor.rowcount}).')

		##################################################

		log.info('Committing changes.')
		db.commit()

	log.info('Finished running.')
	print('Finished running.')

if __name__ == '__main__':
    
    # We send a notification to email after the process finnish
    if len(sys.argv) == 1:
        main("")
        send_notification("all")
    else:
        main(sys.argv[1])
        send_notification(sys.argv[1])