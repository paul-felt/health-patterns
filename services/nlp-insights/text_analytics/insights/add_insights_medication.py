import logging

import uuid
import collections
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.coding import Coding
from fhir.resources.dosage import Dosage, DosageDoseAndRate
from fhir.resources.extension import Extension
from fhir.resources.medicationstatement import MedicationStatement
from fhir.resources.reference import Reference
from fhir.resources.identifier import Identifier
from fhir.resources.adverseevent import AdverseEvent
from fhir.resources.adverseevent import AdverseEventSuspectEntity
from fhir.resources.adverseevent import AdverseEventSuspectEntityCausality
from fhir.resources.quantity import Quantity
from fhir.resources.timing import Timing
from text_analytics.insights import insight_constants
from text_analytics.utils import fhir_object_utils

logger = logging.getLogger()

def _create_med_statement_from_template():

    med_statement_template = {
        "status": "unknown",
        "id": str(uuid.uuid4()),  # TODONOW: how should we set this?
        "medicationCodeableConcept": {

            "text": "template"
        }
    }
    med_statement = MedicationStatement.construct(**med_statement_template)
    return med_statement

def _create_adverse_event_from_template(condition_name, code, source, is_confirmed):

    type_template = {
        "text": f"{condition_name}",
        "coding": [Coding.construct(**{f"system": source, "code": code, "display": condition_name})]
    }
    ade_type = CodeableConcept.construct(**type_template)

    id_template = {
        "type": ade_type,
        "system": source,
        "value": code
    }
    med_statement_template = {
        "identifier": Identifier.construct(**id_template),
        "actuality": "actual" if is_confirmed else "potential",  # TODONOW: consider mapping ade_discussed to "potential"
    }

    med_statement = AdverseEvent.construct(**med_statement_template)
    return med_statement

def _build_resource(nlp, diagnostic_report, nlp_output, span_to_medref = None):
    concepts = nlp_output.get('concepts')
    med_statements_found = {}            # key is UMLS ID, value is the FHIR resource
    med_statements_insight_counter = {}  # key is UMLS ID, value is the current insight_num

    if hasattr(nlp, 'add_medications'):
        med_statements_found, med_statements_insight_counter = nlp.add_medications(nlp, diagnostic_report, nlp_output, med_statements_found, med_statements_insight_counter, span_to_medref)

    for concept in concepts:
        the_type = concept['type']
        if isinstance(the_type, str):
            the_type = [the_type]
        if len(set(the_type) & set(['umls.Antibiotic', 'umls.ClinicalDrug', 'umls.PharmacologicSubstance', 'umls.OrganicChemical'])) > 0:
            med_statements_found, med_statements_insight_counter = create_insight(concept, nlp, nlp_output, diagnostic_report, _build_resource_data, med_statements_found, med_statements_insight_counter, span_to_medref)

    if len(med_statements_found) == 0:
        return None
    return list(med_statements_found.values())

def create_insight(concept, nlp, nlp_output, diagnostic_report, build_resource, med_statements_found, med_statements_insight_counter, span_to_med_refs=None):
    cui = concept.get('cui')
    med_statement = med_statements_found.get(cui)
    if med_statement is None:
        med_statement = _create_med_statement_from_template()
        med_statement.meta = fhir_object_utils.add_resource_meta_unstructured(nlp, diagnostic_report)
        med_statements_found[cui] = med_statement
        insight_num = 1
    else:
        insight_num = med_statements_insight_counter[cui] + 1
    med_statements_insight_counter[cui] = insight_num
    insight_id = "insight-" + str(insight_num)
    build_resource(med_statement, concept, insight_id)
    insight = Extension.construct()
    insight.url = insight_constants.INSIGHT_INSIGHT_ENTRY_URL
    insight_id_ext = fhir_object_utils.create_insight_extension(insight_id, insight_constants.INSIGHT_ID_UNSTRUCTURED_SYSTEM)
    insight.extension = [insight_id_ext]
    insight_detail = fhir_object_utils.create_insight_detail_extension(nlp_output)
    insight.extension.append(insight_detail)
    insight_span = fhir_object_utils.create_insight_span_extension(concept)
    insight.extension.append(insight_span)
    insight_model_data = concept.get('insightModelData')
    if insight_model_data is not None:
        fhir_object_utils.add_medication_confidences(insight.extension, insight_model_data)
    result_extension = med_statement.meta.extension[0]
    result_extension.extension.append(insight)

    # HACK: populate a span->medication ref map for later use in ADE
    if span_to_med_refs is not None:
        span_to_med_refs[(concept['begin'],concept['end'])].append(med_statement)

    return med_statements_found, med_statements_insight_counter

def _build_resource_data(med_statement, concept, insight_id):
    if med_statement.status is None:
        med_statement.status = 'unknown'

    drug = concept.get('preferredName')

    if type(med_statement.medicationCodeableConcept) is dict and med_statement.medicationCodeableConcept.get("text") == "template":
        codeable_concept = CodeableConcept.construct()
        codeable_concept.text = drug
        med_statement.medicationCodeableConcept = codeable_concept
        codeable_concept.coding = []

    fhir_object_utils.add_codings_drug(concept, drug, med_statement.medicationCodeableConcept, insight_id, insight_constants.INSIGHT_ID_UNSTRUCTURED_SYSTEM)


def create_med_statements_from_insights(nlp, diagnostic_report, nlp_output, span_to_medref):
    med_statements = _build_resource(nlp, diagnostic_report, nlp_output, span_to_medref)
    if med_statements is not None:
        for med_statement in med_statements:
            med_statement.subject = diagnostic_report.subject
            fhir_object_utils.create_derived_resource_extension(med_statement)
    return med_statements

def multi_stage_getattr(root_object, *keys):
    """
    Convenience method to allow a caller to do a nested getattr check on an object.  Instead of having
    to call hasattr() one level at a time, a caller may pass in a list of keys to be vetted sequentially.
    :param root_object:
    :param keys:
    :return:
    """
    test_object = root_object
    for key in keys:
        if key not in test_object:
            return None
        test_object = test_object[key]
    return test_object

def create_adverse_events_from_insights(nlp, diagnostic_report, nlp_output, span_to_medref):
    uid_to_covered_text = {}
    for sentence in nlp_output['sentences']:
        if 'uid' in sentence:
            uid_to_covered_text[sentence['uid']] = sentence['coveredText']

    adverse_events = []
    for adverse_event_attr in nlp_output['attributeValues']:
        begin, end = adverse_event_attr['begin'], adverse_event_attr['end']
        evidence_uid = adverse_event_attr.get('evidenceSpans', [{'uid':None}])[0]['uid']
        evidence = uid_to_covered_text.get(evidence_uid, "Not available")
        medication_name = adverse_event_attr.get('preferredName',"Not available")
        if (begin,end) in span_to_medref:
            med_statement = span_to_medref[(begin,end)]
            if adverse_event_attr['name'] == "MedicationAdverseEvent":
                considering_score = multi_stage_getattr(adverse_event_attr, 'insightModelData', 'medication', 'adverseEvent', 'usage', 'consideringScore')
                # get meddra code (if available)
                meddra = [[{'prefName':'Not available', 'meddraCode':'Not available'}]] # default
                adverse_event_mods = multi_stage_getattr(adverse_event_attr, 'insightModelData', 'medication', 'modifiers', 'associatedAdverseEvents')
                if adverse_event_mods is not None:
                    for adverse_event_mod in adverse_event_mods:
                        if 'meddraCodes' in adverse_event_mod:
                            meddra = adverse_event_mod['meddraCodes']

                ade_template = _create_adverse_event_from_template(
                    condition_name=meddra[0][0]['prefName'],
                    code=meddra[0][0]['meddraCode'], source='meddra', is_confirmed = considering_score<0.5)
                # the subject (Patient) is the same as for the diagnostic report that it came from
                ade_template.subject = diagnostic_report.subject
                # the suspectEntity is the same as for the diagnostic report that it came from
                med_reference = Reference.construct(reference=f"urn:uuid:{med_statement[0].id}")

                causalityString = f"Target medication = '{medication_name}'. Evidence = '{evidence}'"
                suspectEntityReference = AdverseEventSuspectEntity.construct(**{
                    "instance": med_reference,
                    "causality": [AdverseEventSuspectEntityCausality.construct(productRelatedness=causalityString)]
                })
                ade_template.suspectEntity = [suspectEntityReference]
                adverse_events.append(ade_template)


    return adverse_events
