import collections
import logging
from collections import namedtuple
from enum import Enum

import asyncio
import aiohttp
import tqdm
import ujson
from aiohttp import web
from elasticsearch_dsl import Q

# Get an instance of a logger
logger = logging.getLogger(__name__)
MAX_RECORDS = 10
Result = namedtuple('Result', 'status data')
HTTPStatus = Enum('Status', 'ok not_found error')
DEFAULT_CONCUR_REQ = 5
MAX_CONCUR_REQ = 1000
AWS_AUTHOR_API = 'https://ie4djxzt8j.execute-api.eu-west-1.amazonaws.com/coding'


# custom exception
class FetchError(Exception):
    def __init__(self, id, error):
        self.id = id
        self.error = error


# add author_name and query value into book details
def add_additional_data_to_record(data, qry, book_details):
    book_details['_source']['author'] = data['author']
    book_details['_source']['query'] = qry


class BookCoroutineService:
    """
    It provides Coroutine service,
    take one query at a time and while waiting to get author_name,
    use other query and start.
    """

    # generic method
    @asyncio.coroutine
    def call_service(self, base_url, data, method):
        url = base_url
        response = None
        if method == 'POST':
            try:
                response = yield from aiohttp.ClientSession(json_serialize=ujson.dumps).post(url, json=data)
            except aiohttp.ClientConnectorError as ce:
                logger.debug('ClientConnectorError: ' + str(ce.strerror))
                message = "call author aws service: " + ce.strerror
                raise Exception(message)
            except Exception as e:
                logger.debug('call_service, Exception: ' + str(e))
                raise Exception(e)

        if response.status != 200:
            logger.debug('call_service, ClientResponseError: ' + str(response.reason))
            raise aiohttp.ClientResponseError(code=response.status, message=response.reason, headers=response.headers)

        data = yield from response.json()
        return data

    @asyncio.coroutine
    def retrieve_author_details(self, book_details, qry, semaphore, verbose):

        base_url = AWS_AUTHOR_API
        book_id = book_details['_source']['id']
        data = {'book_id': book_id}

        try:
            with (yield from semaphore):
                data = yield from self.call_service(base_url, data, 'POST')
        except web.HTTPNotFound as wh:
            logger.debug('retrieve_author_details, HTTPNotFound: ' + str(wh))
            status = HTTPStatus.not_found
        except Exception as exc:
            logger.debug('retrieve_author_details, Exception: ' + str(exc))
            raise Exception(exc)
        else:
            # to handle the I/O blocking while saving data
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, add_additional_data_to_record, data, qry, book_details)
            status = HTTPStatus.ok

        return Result(status, book_details)

    # generic function
    @asyncio.coroutine
    def schedule_services(self, records_list, qry, next_calling_service, verbose, concur_req):

        counter = collections.Counter()
        semaphore = asyncio.Semaphore(concur_req)
        to_do = [next_calling_service(item_details, qry, semaphore, verbose) for item_details in records_list]
        to_do_iter = asyncio.as_completed(to_do)

        if not verbose:
            to_do_iter = tqdm.tqdm(to_do_iter, total=len(records_list))

        for future in to_do_iter:
            try:
                res = yield from future
            except FetchError as exc:
                logger.debug('schedule_services, FetchError: ' + str(exc))
                id = exc.id
                try:
                    error_msg = exc.__cause__.args[0]
                except IndexError:
                    error_msg = exc.__cause__.__class__.__name__
                if verbose and error_msg:
                    msg = 'Error for {}: {}'
                    logger.debug('schedule_services, FetchError: ' + msg.format(id, error_msg))
                status = HTTPStatus.error
            else:
                status = res.status
            counter[status] += 1
        return counter

    # retrieve author_name for all book_id
    def call_author_many(self, records_list, qry, verbose, concur_req):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        coro = self.schedule_services(records_list, qry, self.retrieve_author_details, verbose, concur_req)
        loop.run_until_complete(coro)

        loop.close()


class ElasticSearchBookService:
    """
    make elasticseach instance using document_class_name
    holds query_list, size and search_instance
    query_list:- list of query/keyword, on which summary will filter
    size:- length of filtered result
    """

    def __init__(self, document_class_name, query_list, size):
        self.query_list = query_list
        self.size = size
        self.search_instance = document_class_name.search()

    """ 
    filter summaries using query from given query list
    """
    def run_query_list(self):
        result = []
        book_coroutine_service = BookCoroutineService()

        # to filter using each query/keyword, iterate the query_list
        for qry in self.query_list:

            # define elastic search query, where summary field must match with given query/keyword
            q = Q('bool', must=[Q('match', summary=qry), ])

            # adding elastic search query,
            # sort filtered result based on _score,
            # result's length start from 0 to self.size
            search_with_query = self.search_instance.query(q).sort('_score')[0:self.size]

            # execute elastic query
            response = search_with_query.execute()
            list_doc = response.to_dict()['hits']['hits']

            # call coroutine service on each filtered result to get author for each book_id
            book_coroutine_service.call_author_many(list_doc, qry, DEFAULT_CONCUR_REQ, MAX_CONCUR_REQ)
            result.append(response.to_dict()['hits']['hits'])
        return result