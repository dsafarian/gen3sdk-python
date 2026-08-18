## FHIR

The fhir commands can be invoked as follows

`gen3 fhir COMMAND [ARGS] [OPTIONS]`

For a list of commands and options run

`gen3 fhir --help`

For example, the following tags the 'Patient.ndjson' file with Gen3 authorization and outputs 'gen3_Patient.ndjson' using the authorization rules from 'config.yaml'

`gen3 fhir transform Patient.ndjson gen3_Patient.ndjson config.yaml --batch_size 10000 --workers 8 `

