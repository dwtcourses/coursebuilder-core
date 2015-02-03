# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DB Entity and classes to manage the creation and modification of clusters.
"""

__author__ = 'Milagro Teruel (milit@google.com)'

import appengine_config
import collections
import json
import math
import os
import urllib
import zlib

from mapreduce import context

from common import schema_fields
from controllers import utils
from models import jobs
from models import models
from models import transforms
from models import data_sources
from models.entities import BaseEntity
from modules.analytics import student_aggregate
from modules.dashboard import student_answers_analytics
from modules.dashboard import dto_editor

from google.appengine.ext import db


DIMENSION_TYPE_UNIT = 'u'
DIMENSION_TYPE_LESSON = 'l'
DIMENSION_TYPE_QUESTION = 'q'

# All of the possible fields that can be in a dimension
DIM_TYPE = 'type'
DIM_ID = 'id'
DIM_HIGH = 'high'  # The upper bound. Optional
DIM_LOW = 'low'  # The lower bound. Optional
DIM_EXTRA_INFO = 'extra-info'  # Optional
DIM_VALUE = 'value'  # For students vectors. Optional


class ClusterEntity(BaseEntity):
    """Representation of a cluster used for clasification of students.

    A cluster is defined by a set of dimensions and a range of numeric values
    for each dimension. For dimensions with boolean values, they must be
    converted to a numeric representation. The identifier for a dimension is
    the type (unit, lesson, question...) plus the id of this type.

    The attribute data contains a json dictionary with the following structure:
    {
        'name': 'string with name of cluster',
        'description': 'string with description of the cluster',
        'vector': [{dictionary dimension 1}, {dictionary dimension 2}, ... ]
    }
    The value of 'vector' is a list with one dictionary for each dimension.

    Example of dimension:
        {
        clustering.DIM_TYPE: clustering.DIMENSION_TYPE_UNIT,
        clustering.DIM_ID: 1,
        clustering.DIM_LOW: 0,
        clustering.DIM_HIGH: 50,
        clustering.DIM_EXTRA_INFO: ''
        }

    Dimension-extra-info is a field for any information needed to
    calculate the value of the dimension. It is also a json dictionary.

    The same question can be used several times inserting it into different
    units or lessons. we distinguish this different uses, and consequently
    the id of a question dimension is constructed also with the ids of the
    unit and lesson in wich the question was found. To get this id, use the
    function pack_question_dimid. The inverse function is
    unpack_question_dimid.
    A question can also appear several times in the same unit and lesson. In
    that case, we consider all usages as a single question dimension for
    compatibility with the information in StudentAggregateEntity.
    """
    data = db.TextProperty(indexed=False)


def pack_question_dimid(unit_id, lesson_id, question_id):
    """Constructs the dimension id for a question using unit and lesson id.

    Args:
        unit_id: a number or string indicating the unit id.
        lesson_id: a number, string or None indicating the lesson id.
        question_id: a number or string indicating the question id.

    Returns:
        A string with the dimension id."""
    return ':'.join((str(unit_id), str(lesson_id), str(question_id)))


def unpack_question_dimid(dimension_id):
    """Decompose the dimension id into unit, lesson and question id.

    Returns:
        A tuple unit_id, lesson_id, question_id.
        unit_id and question_id are strings. lesson_id can be a string or
        None.
    """
    unit_id, lesson_id, question_id = dimension_id.split(':')
    if lesson_id == 'None':
        lesson_id = None
    return unit_id, lesson_id, question_id


class ClusterDTO(object):
    """Data transfer object for ClusterEntity."""

    def __init__(self, the_id, the_dict):
        self.id = the_id
        self.dict = the_dict

    @property
    def name(self):
        return self.dict.get('name', '')

    @property
    def description(self):
        return self.dict.get('description', '')

    @property
    def vector(self):
        return self.dict.get('vector', [])


class ClusterDAO(models.BaseJsonDao):
    DTO = ClusterDTO
    ENTITY = ClusterEntity
    ENTITY_KEY_TYPE = models.BaseJsonDao.EntityKeyTypeId


class ClusterDataSource(data_sources.SynchronousQuery):
    """Gets the information of the available clusters in the course.

    Renders the jinja template clustering.html.
    """

    @staticmethod
    def fill_values(app_context, template_values):
        """Sets values into the dict used to fill out the Jinja template."""
        template_values['clusters'] = ClusterDAO.get_all()
        edit_urls = []
        for cluster in template_values['clusters']:
            params = urllib.urlencode({
                'action' : 'edit_cluster',
                'key': cluster.id})
            edit_urls.append('dashboard?{}'.format(params))
        template_values['edit_urls'] = edit_urls


def _has_right_side(dim):
    """Returns True if the value of dim[DIM_HIGH] is not None or ''."""
    return dim.get(DIM_HIGH) != None and dim.get(DIM_HIGH) != ''


def _has_left_side(dim):
    """Returns True if the value of dim[DIM_LOW] is not None or ''."""
    return dim.get(DIM_LOW) != None and dim.get(DIM_LOW) != ''


def get_possible_dimensions(app_context):
    """Returns a list of dictionaries with all possible dimensions.

    Any scored unit, lessons, assessment or question can be a dimension. If a
    question is used in differents units and lessons, then a dimension will
    be created for each use of the question. However, if the question in used
    twice or more in the same unit and lesson, then only one dimension will
    be created for this question, unit and lesson.

    For more details in the structure of dimensions see ClusterEntity
    documentation.
    """
    datasource = student_answers_analytics.OrderedQuestionsDataSource()
    template_values = {}
    # This has extra information but it was already implemented.
    # Also, the OrderedQuestionsDataSource takes care of the case
    # where assessments are used as pre- or post- items in Units, so
    # we don't have to code for that case here.
    datasource.fill_values(app_context, template_values)
    result = []
    for unit in template_values['units']:
        unit_scored_lessons = 0
        unit_dict = {
            DIM_TYPE: DIMENSION_TYPE_UNIT,  # Unit or assessment
            DIM_ID: unit['unit_id'],
            'name': unit['title']}  # Name won't be saved in ClusterEntity
        result.append(unit_dict)
        # The content of an assessment is indicated by a lesson_id of None.
        # Inside that lesson we can find all the questions added directly
        # to the assessment.
        for item in unit['contents']:
            lesson_id = item.get('lesson_id')
            # A unit may have a pre or post assessment, in that case the item
            # has unit_id, not a lesson_id.
            included_assessment_id = item.get('unit_id')
            lesson_title = item.get('title')
            if lesson_title and lesson_id and item.get('tallied'):
                result.append({
                    DIM_TYPE: DIMENSION_TYPE_LESSON,
                    DIM_ID: lesson_id,
                    'name': lesson_title})
                unit_scored_lessons += 1
            elif included_assessment_id and lesson_title:
                result.append({
                    DIM_TYPE: DIMENSION_TYPE_UNIT,
                    DIM_ID: included_assessment_id,
                    'name': lesson_title})
                unit_scored_lessons += 1
            # If lesson is not tallied (graded) is not considered a dimension
            for question in item['questions']:
                if included_assessment_id:
                    question_id = pack_question_dimid(
                        included_assessment_id, None, question['id'])
                else:
                    question_id = pack_question_dimid(
                        unit['unit_id'], lesson_id, question['id'])
                result.append({
                    DIM_TYPE: DIMENSION_TYPE_QUESTION,
                    DIM_ID: question_id,
                    'name': question['description']})
        # This should affect the result list as well.
        unit_dict[DIM_EXTRA_INFO] = json.dumps(
            {'unit_scored_lessons': unit_scored_lessons})
    return result


class ClusterRESTHandler(dto_editor.BaseDatastoreRestHandler):
    """REST Handler for ClusterEntity model."""

    URI = '/rest/cluster'

    XSRF_TOKEN = 'cluster-edit'

    DAO = ClusterDAO

    SCHEMA_VERSIONS = ['1.0']

    REQUIRED_MODULES = []

    EXTRA_JS_FILES = []
    EXTRA_CSS_FILES = []
    ADDITIONAL_DIRS = [os.path.join(
        appengine_config.BUNDLE_ROOT, 'modules', 'analytics')]

    @staticmethod
    def pack_id(dim_id, dim_type):
        """Concatenates the id and type of the dimension"""
        return '{}---{}'.format(dim_id, dim_type)

    @staticmethod
    def unpack_id(packed_id):
        """Unpacks the id and type of the dimension"""
        return packed_id.split('---')

    @classmethod
    def get_schema(cls, app_context=None):
        cluster_schema = schema_fields.FieldRegistry(
            'Cluster Definition',
            description='cluster definition',
            extra_schema_dict_values={'className': 'cluster-container'})
        cluster_schema.add_property(schema_fields.SchemaField(
            'version', '', 'string', optional=True, hidden=True))
        cluster_schema.add_property(schema_fields.SchemaField(
            'name', 'Name', 'string', optional=False,
            extra_schema_dict_values={'className': 'cluster-name'}))
        cluster_schema.add_property(schema_fields.SchemaField(
            'description', 'Description', 'string', optional=True,
            extra_schema_dict_values={'className': 'cluster-description'}))

        dimension = schema_fields.FieldRegistry('Dimension',
            extra_schema_dict_values={'className': 'cluster-dim'})
        if app_context:
            dimensions = get_possible_dimensions(app_context)
            to_select = [(cls.pack_id(dim[DIM_ID], dim[DIM_TYPE]), dim['name'])
                         for dim in dimensions]
        else:
            to_select = []

        dimension.add_property(schema_fields.SchemaField(
            DIM_ID, 'Dimension Name', 'string', i18n=False,
            extra_schema_dict_values={'className': 'cluster-dim-name'},
            select_data=to_select))
        dimension.add_property(schema_fields.SchemaField(
            DIM_LOW, 'Lower Score', 'string', i18n=False,
            optional=True,
            extra_schema_dict_values={'className': 'cluster-dim-range'}))
        dimension.add_property(schema_fields.SchemaField(
            DIM_HIGH, 'Higher Score', 'string', i18n=False,
            optional=True,
            extra_schema_dict_values={'className': 'cluster-dim-range'}))

        dimension_array = schema_fields.FieldArray(
            'vector', '', item_type=dimension,
            extra_schema_dict_values={
                'className': 'cluster-dim-container',
                'listAddLabel': 'Add a dimension',
                'listRemoveLabel': 'Delete dimension'})

        cluster_schema.add_property(dimension_array)
        dimension.add_property(schema_fields.SchemaField(
            DIM_EXTRA_INFO, '', 'string', i18n=False, hidden=True,
            optional=True))

        return cluster_schema

    def get_default_content(self):
        return {
            'version': self.SCHEMA_VERSIONS[0],
            'name': '',
            'description': '',
            'vector': []}

    def transform_for_editor_hook(self, item_dict):
        """Packs the id and type for the select field in the html."""
        for dim in item_dict['vector']:
            dim[DIM_ID] = ClusterRESTHandler.pack_id(dim[DIM_ID],
                                                     dim[DIM_TYPE])
        return item_dict

    def validate(self, item_dict, key, schema_version, errors):
        """Validates the user input.

        The cluster must:
            - Have a name
            - Have numeric values for the fields low and high of all
            dimensions.
            - Have a smaller value in the low field than in the high field.
        This function completes the low and high ranges with None values. Also
        divides the id from the select into id and type.
        """
        if not item_dict['name']:
            errors.append('Empty name.')
        error_str = ('Non numeric value in dimension '
                     'range (dimension number {}).')
        # Convert to float and complete the missing ranges with None.
        for index, dim in enumerate(item_dict['vector']):
            if _has_right_side(dim):
                try:
                    dim[DIM_HIGH] = float(dim[DIM_HIGH])
                except ValueError:
                    errors.append(error_str.format(index))
            else:
                dim[DIM_HIGH] = None
            if _has_left_side(dim):
                try:
                    dim[DIM_LOW] = float(dim[DIM_LOW])
                except ValueError:
                    errors.append(error_str.format(index))
            else:
                dim[DIM_LOW] = None
            if (_has_left_side(dim) and _has_right_side(dim)
                and dim[DIM_HIGH] < dim[DIM_LOW]):
                errors.append('Wrong range interval in dimension'
                              'number {}'.format(index))
            # Unpack the select id.
            dim[DIM_ID], dim[DIM_TYPE] = ClusterRESTHandler.unpack_id(
                dim[DIM_ID])

    def pre_save_hook(self, dto):
        """Filter out dimensions with missing start- and end- range."""
        dto.dict['vector'] = [dim for dim in dto.dict['vector']
                              if _has_left_side(dim) or _has_right_side(dim)]


class StudentVector(BaseEntity):
    """Representation of a single student based on a fixed set of dimensions.

    The attribute vector stores the value of the student for each possible
    dimension. This value must be a number, and it is generated by the job
    StudentVectorGenerator. The information is organized in a dictionary,
    for example:
        {
            DIM_TYPE: clustering.DIMENSION_TYPE_QUESTION,
            DIM_ID: 3,
            DIM_VALUE: 60
        }
    """
    vector = db.TextProperty(indexed=False)
    # TODO(milit): add a data source type so that all entities of this type
    # can be exported via data pump for external analysis.

    @classmethod
    def safe_key(cls, db_key, transform_fn):
        return db.Key.from_path(cls.kind(), transform_fn(db_key.id_or_name()))

    @staticmethod
    def get_dimension_value(vector, dim_id, dim_type):
        """Returns the value of the dimension with the given id and type.

        Return None if there is no matching dimension.

        Args:
            vector: A list of dictionaries. Corresponds to the StudentVector
            vector attribute unpacked.
        """
        candidates = [dim[DIM_VALUE] for dim in vector
                      if dim[DIM_ID] == dim_id and dim[DIM_TYPE] == dim_type]
        if candidates:
            return candidates[0]


class StudentClusters(BaseEntity):
    """Representation of the relation between StudentVector and ClusterEntity.

    There is a StudentClusters entity for each StudentVector, created by the
    ClusteringGenerator job. The key name corresponds to the key_name of the
    StudentVector entity.
    The attribute clusters is a dictionary mapping ClusterEntity ids to
    distance values for a given distance type (Hamming as default). This
    distances are claculated using the job ClusteringGenerator. For example:
        {'1': 3, '2': 0, ... }
    """
    clusters = db.TextProperty(indexed=False)

    @classmethod
    def safe_key(cls, db_key, transform_fn):
        return db.Key.from_path(cls.kind(), transform_fn(db_key.id_or_name()))


class StudentVectorGenerator(jobs.MapReduceJob):
    """A map reduce job to create StudentVector based on StudentAggregateEntity.

    This job updates the vector field in the associated StudentVector, or
    creates a new one if there is none. This vector has a value for each
    dimension type, calculated as follows:
        Questions: The last weighted score of the question.
        Lessons: The last weighted score of the lesson.
        Units or Assessments: The average of all the scored lessons in
            the unit or assessment. If no lessons, then the unit has a score
            by itself.

    NOTE: StudentAggregateEntity is created by the job
    StudentAggregateGenerator, so they have to run one after the other.
    """

    # This dictionary maps each dimension type to a function that extracts
    # its value from a StudentAggregateEntity data field. The function receives
    # two arguments, the data relevant to the dimension as list of
    # dictionaries and the dimension dictionary. The data is the output of the
    # function _inverse_submission_data.
    # To define a new dimension type you must define the function and include
    # it here. That way we avoid changing the map function.
    DIMENSION_FUNCTIONS = {
        DIMENSION_TYPE_QUESTION: '_get_question_score',
        DIMENSION_TYPE_LESSON: '_get_lesson_score',
        DIMENSION_TYPE_UNIT: '_get_unit_score'
    }

    @classmethod
    def get_function_for_dimension(cls, dimension_type):
        """Returns the function to calculate the score of a dimension type.

        The mapping between dimension types and function names is in the
        class attribute DIMENSION_FUNCTIONS."""
        return getattr(cls, cls.DIMENSION_FUNCTIONS[dimension_type],
                       lambda x, y: 0)

    @staticmethod
    def get_description():
        return 'StudentVector generation'

    @classmethod
    def entity_class(cls):
        return student_aggregate.StudentAggregateEntity

    def build_additional_mapper_params(self, app_context):
        return {'possible_dimensions': get_possible_dimensions(app_context)}

    @staticmethod
    def map(item):
        """Updates the values in vector.

        Creates a new StudentVector using the id of the item, a
        StudentAggregateEntity. Calculates the value for every dimension
        from the assessment data in item.
        """
        mapper_params = context.get().mapreduce_spec.mapper.params
        raw_data = transforms.loads(zlib.decompress(item.data))
        raw_data = raw_data.get('assessments', [])
        if not raw_data:
            return
        data = StudentVectorGenerator._inverse_submission_data(
            mapper_params['possible_dimensions'], raw_data)
        vector = []
        for dim in mapper_params['possible_dimensions']:
            data_for_dimension = data[dim[DIM_TYPE], dim[DIM_ID]]
            value = StudentVectorGenerator.get_function_for_dimension(
                        dim[DIM_TYPE])(data_for_dimension, dim)
            new_dim = {
                DIM_TYPE: dim[DIM_TYPE],
                DIM_ID: dim[DIM_ID],
                DIM_VALUE: value}
            vector.append(new_dim)
        StudentVector(key_name=str(item.key().name()),
                      vector=transforms.dumps(vector)).put()

    @staticmethod
    def reduce(item_id, values):
        """Empty function, there is nothing to reduce."""
        pass

    @staticmethod
    def _inverse_submission_data(dimensions, raw_data):
        """Build a dictionary with the information from raw_data by dimension.

        For each dimension builds an entry in the result. The value is a list
        with all the submissions relevant to that dimension. The concept of
        relevant is different for each type of dimension. For example, for a
        unit the relevant data are the submissions of all lessons for that
        unit.

        Returns:
            An instance of defaultdict with default empty list."""
        result = collections.defaultdict(lambda: [])
        for activity in raw_data:
            activity_lesson = activity.get('lesson_id')
            activity_unit = activity.get('unit_id')
            # This creates aliasing but it's fine beacuse is read only.
            # It only adds a copy of the timestamp for the questions.
            result[DIMENSION_TYPE_UNIT, activity_unit].append(activity)
            result[DIMENSION_TYPE_LESSON, activity_lesson].append(activity)
            for submission in activity.get('submissions', []):
                for answer in submission.get('answers', []):
                    question_id = answer.get('question_id')
                    answer['timestamp'] = submission['timestamp']
                    dim_id = pack_question_dimid(activity_unit,
                                                 activity_lesson, question_id)
                    result[DIMENSION_TYPE_QUESTION, dim_id].append(answer)
        return result

    @staticmethod
    def _get_question_score(data, unused_dimension):
        """The score of a question is the last weighted score obtained.

        If a question in present multiple times in the same submission, then
        the score is the average weighted score of the question in that
        submission. If there is no submission for the question the score is 0.

        Args:
            data: a list of dictionaries.
        """
        last_scores = []
        last_timestamp = 0
        for answer in data:
            # Could be more than one question with the same timestamp
            score = answer.get('weighted_score')
            if score and answer['timestamp'] > last_timestamp:
                last_scores = [score]
                last_timestamp = answer['timestamp']
            elif score and answer['timestamp'] == last_timestamp:
                last_scores.append(score)
        if last_scores:
            return math.fsum(last_scores) / len(last_scores)
        return 0

    @staticmethod
    def _get_lesson_score(data, dimension):
        """The score of a lesson is its last score."""
        for submission in data:
            if ('lesson_id' in submission and 'last_score' in submission
                and submission['lesson_id'] == dimension[DIM_ID]):
                return submission['last_score']
        return 0

    @staticmethod
    def _get_unit_score(data, dimension):
        """The score of a unit is the average score of its scored lessons.

        If the unit has no lessons (assessment), the unit will have its
        own score.
        """
        if not DIM_EXTRA_INFO in dimension:
            scored_lessons = 1
        else:
            extra_info = json.loads(dimension[DIM_EXTRA_INFO])
            if not 'unit_scored_lessons' in extra_info:
                scored_lessons = 1
            else:
                scored_lessons = max(extra_info['unit_scored_lessons'], 1)
        score = 0
        for submission in data:
            if ('unit_id' in submission and 'last_score' in submission
                and submission['unit_id'] == dimension[DIM_ID]):
                score += submission['last_score']
        return score/float(scored_lessons)


def hamming_distance(vector, student_vector):
    """Return the hamming distance between a ClusterEntity and a StudentVector.

    The hamming distance between an ClusterEntity and a StudentVector is the
    number of dimensions in which the student value is not inside the vector
    range. If a dimension is not present in the student vector, we assume its
    value is 0. If a dimension is not present in the cluster_value, we assume
    that every value is included in the range.

    Params:
        vector: the vector field of a ClusterEntity instance.
        student_vector: the vector field of a StudentVector instance.
    """
    def fits_left_side(dim, value):
        """_has_left_side(dim) -> dim[DIM_LOW] <= value"""
        return not _has_left_side(dim) or dim[DIM_LOW] <= value

    def fits_right_side(dim, value):
        """_has_right_side(dim) -> dim[DIM_HIGH] >= value"""
        return not _has_right_side(dim) or dim[DIM_HIGH] >= value

    distance = 0
    for dim in vector:
        value = StudentVector.get_dimension_value(student_vector,
                                                  dim[DIM_ID], dim[DIM_TYPE])
        if not value:
            value = 0
        if not fits_left_side(dim, value) or not fits_right_side(dim, value):
            distance += 1
    return distance


class ClusteringGenerator(jobs.MapReduceJob):
    """A map reduce job to calculate which students belong to each cluster.

    This job calculates the distance between each StudentVector and each
    ClusterEntity using the Hamming distance. The value of the distance is
    going to be stored in the StudentVector attibute clusters. This attribute
    is a json dictionary where the keys are the ids (as strings) of the
    clusters and the values are the distances. All previous distances are
    discarded.

    All distances that don't fall in the range (MIN_DISTANCE, MAX_DISTANCE)
    are ignored and not stored in the StudentVector entity.

    In the reduce step it returns calculated two statistics: the number of
    students in each cluster and the intersection of pairs of clusters.
    """
    MIN_DISTANCE = 0
    MAX_DISTANCE = 2

    # TODO(milit): Add settings to disable heavy statistics.
    @staticmethod
    def get_description():
        return 'StudentVector clusterization'

    @classmethod
    def entity_class(cls):
        return StudentVector

    def build_additional_mapper_params(self, app_context):
        clusters = [{'id': cluster.id, 'vector': cluster.vector}
                    for cluster in ClusterDAO.get_all()]
        return {
            'clusters': clusters,
            'max_distance': getattr(self, 'MAX_DISTANCE', 2)
        }

    @staticmethod
    def map(item):
        """Calculates the distance from the StudentVector to ClusterEntites.

        Stores this distances in the clusters attibute of item. Ignores
        distances not in range (MIN_DISTANCE, MAX_DISTANCE).

        Yields:
            Pairs (key, value). There are two types of keys:
                1.  A cluster id: the value is a tuple (student_id, distance).
                2.  A pair of clusters ids: the value is a 3-uple
                    (student_id, distance1, distance2)
                    distance1 is the distance from the student vector to the
                    cluster with the first id of the tuple and distance2 is
                    the distance to the second cluster in the tuple.
            One result is yielded for every cluster id and pair of clusters
            ids. If (cluster1_id, cluster2_id) is yielded, then
            (cluster2_id, cluster1_id) won't be yielded.
        """
        mapper_params = context.get().mapreduce_spec.mapper.params
        max_distance = mapper_params['max_distance']
        clusters = {}
        item_vector = transforms.loads(item.vector)
        for cluster in mapper_params['clusters']:
            distance = hamming_distance(cluster['vector'], item_vector)
            if distance > max_distance:
                continue
            for cluster2_id, distance2 in clusters.items():
                key = transforms.dumps((cluster2_id, cluster['id']))
                value = (item.key().name(), distance, distance2)
                yield (key, transforms.dumps(value))
            clusters[cluster['id']] = distance
            to_yield = (item.key().name(), distance)
            yield(cluster['id'], transforms.dumps(to_yield))
        clusters = transforms.dumps(clusters)
        StudentClusters(key_name=item.key().name(), clusters=clusters).put()

    @staticmethod
    def reduce(item_id, values):
        """
        This function can take two types of item_id (as json string).
            A number: the values are 2-uples (student_id, distance) and is
            used to calculate a count statistic.
            A list: the item_id holds the IDs of two clusters and the value
            corresponds to 3-uple (student_id, distance1, distance2). The value
            is used to calculate an intersection stats.

        Yields:
            A json string representing a tuple ('stat_name', {item_id:
            distances}). The i-th number in the distances list corresponds to
            the number of students with distance less or equal to i to
            the vector (or vectors). item_id is the same item_id received as
            a parameter, but converted from the json string.
        """
        item_id = transforms.loads(item_id)
        distances = collections.defaultdict(lambda: 0)
        if isinstance(item_id, list):
            stat_name = 'intersection'
            for value in values:
                value = transforms.loads(value)
                min_distance = max(value[1], value[2])
                distances[min_distance] += 1
            item_id = tuple(item_id)
        else:
            stat_name = 'count'
            for value in values:
                value = transforms.loads(value)
                distances[value[1]] += 1
        distances = dict(distances)
        list_distances = [0] * (max([int(k) for k in distances]) + 1)
        for distance, count in distances.items():
            list_distances[int(distance)] = count
        # Accumulate the distances.
        for index in range(1, len(list_distances)):
            list_distances[index] += list_distances[index - 1]
        yield transforms.dumps((stat_name, (item_id, list_distances)))


class TentpoleStudentVectorDataSource(data_sources.SynchronousQuery):
    """This datasource does not retrieve elements.

    This datasource exists to put a button in the Visualization html that
    allows the user to run the job StudentVectorGenerator and to create the
    StudentVector entities. Also gives information about the state of the
    StudentAggregateGenerator job, which is a requisite to run
    StudentVectorGenerator.

    However, it is NOT expected to retrieve the StudentVector entities for
    display.
    """

    @staticmethod
    def required_generators():
        return [StudentVectorGenerator]

    @staticmethod
    def fill_values(app_context, template_values, unused_gen):
        """Check if the StudentAggregateGenerator has run."""
        job = student_aggregate.StudentAggregateGenerator(app_context).load()
        if not job:
            template_values['message'] = ('The student aggregated job has '
                                          'never run.')
        message_str = ('The student aggregated values where '
                       'last calculated on {}.')
        last_update = getattr(job, 'updated_on', None)
        if not last_update:
            template_values['message'] = ('The student aggregated job has '
                                          'never run.')
        else:
            template_values['message'] = message_str.format(
                job.updated_on.strftime(utils.HUMAN_READABLE_DATETIME_FORMAT))


class ClusterStatisticsDataSource(data_sources.AbstractSmallRestDataSource):
    """Returns the values obtained by ClusteringGenerator."""

    @staticmethod
    def required_generators():
        return [ClusteringGenerator]

    @classmethod
    def get_name(cls):
        return 'cluster_statistics'

    @classmethod
    def get_title(cls):
        return ''  # Not used.

    @classmethod
    def get_schema(cls, unused_app_context, unused_catch_and_log,
                   unused_source_context):
        # Without schema the fetch_values function won't be called.
        return 'List with dummy objects'.split()

    @classmethod
    def fetch_values(cls, app_context, unused_source_context, unused_schema,
                     unused_catch_and_log, unused_page_number,
                     clustering_generator_job):
        """Returns the statistics calculated by clustering_generator_job.

        Returns:
            A list of dictionaries and the page number, always 0. The list
            has three elements:
                1.  The results of the count statistic: A list with one entry
                    per cluster. Each entry is a dictionary like:
                        {'name': cluster_name, 'vectors': list_with_counts}
                2.  The results of the intersection statistics: A two level
                    dictionary with the ids of pairs of clusters. For example:
                        {1: {2: list_with_counts, 3: list_with_counts}, ... }
                    No all pairs are included in this intersection. If an entry
                    is missing is safe to assume that the intersection is 0.
                3.  A dictionary with the maximum distance used
                    in the classification. For example: {'max_distance': 2}
            The i-th number in list_with_counts represents the number of
            students that have a distance less or equal than i to the
            cluster or clusters. For a distance grater than the list lenght,
            then the number of students in equal to the last entry on the
            list.

        """
        results = list(jobs.MapReduceJob.get_results(clustering_generator_job))
        count = {}
        intersection = collections.defaultdict(lambda: {})
        for result in results:
            stat, value = result
            if stat == 'count':
                cluster = ClusterDAO.load(value[0])
                count[value[0]] = {'vectors': value[1], 'name': cluster.name}
            elif stat == 'intersection':
                cluster1, cluster2 = value[0]
                intersection[cluster1].update({cluster2: value[1]})
        # Update missing clusters
        for cluster in ClusterDAO.get_all():
            if cluster.id not in count:
                count[cluster.id] = {'vectors': [0], 'name': cluster.name}
        distances = {'max_distance': ClusteringGenerator.MAX_DISTANCE}
        return [count, dict(intersection), distances], 0  # data, page_number
