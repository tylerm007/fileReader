from __future__ import annotations  # enables Resource self reference
import sqlalchemy
from sqlalchemy import update, insert
import logging
import contextlib
from sqlalchemy import Column, Table, ForeignKey
from sqlalchemy.orm.decl_api import DeclarativeMeta #sqlalchemy.orm.decl_api.DeclarativeMeta
from sqlalchemy.orm import relationships, relationship
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy_utils import get_referencing_foreign_keys
from sqlalchemy import event, MetaData, and_, or_
from sqlalchemy.inspection import inspect
from sqlalchemy import select
from sqlalchemy.sql import text
from flask import jsonify
from sqlalchemy_utils.query_chain import QueryChain
import flask_sqlalchemy
import safrs
from safrs.errors import JsonapiError, ValidationError
from typing import List, Dict, Tuple
import util
import json 
import requests
from config import Config

resource_logger = logging.getLogger("api.customize_api")

db = safrs.DB 
"""this is a safrs db not DB"""
session = db.session  # type: sqlalchemy.orm.scoping.scoped_session

class DotDict(dict):
    """ dot.notation access to dictionary attributes """
    # thanks: https://stackoverflow.com/questions/2352181/how-to-use-a-dot-to-access-members-of-dictionary/28463329
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__
    
class CustomEndpoint():
    """
    Nested CustomEndpoint Definition

        customer = CustomEndpoint(model_class=models.Customer, alias="Customer"
        , fields = [(models.Customer.CompanyName, "Customer Name")] 
        , children = [
            CustomEndpoint(model_class=models.Order, alias = "orders"
                , join_on=models.Order.CustomerId
                , fields = [models.Order.AmountTotal, "Total", models.Order.ShippedDate, "Ship Date"]
                , children = CustomEndpoint(model_class=models.OrderDetail, alias="details"
                    , join_on=models.OrderDetail.OrderId
                    , fields = [models.OrderDetail.Quantity, models.OrderDetail.Amount]
                    , children = CustomEndpoint(model_class=models.Product, alias="data"
                        , join_on=models.OrderDetail.ProductId
                        , fields=[models.Product.UnitPrice, models.Product.UnitsInStock]
                        , isParent=False
                        , isCombined=False
                      )
                   )
                ),
            CustomEndpoint(model_class=models.OrderAudit, alias="orderAudit")  # sibling child
            ]
        )
        result = customer.execute(customer,"", "ALFKI")
        # or
        #result = customer.get(request,"OrderList&OrderList.OrderDetailList&OrderList.OrderDetailList.Product", "ALFKI")
    """

    def __init__(self
                 , model_class: DeclarativeMeta | None
                 , alias: str = ""
                 , fields: list[tuple[Column, str] | Column] = []
                 , children: list[CustomEndpoint] | CustomEndpoint = []
                 , join_on: list[tuple[Column] | Column] = None
                 , calling: callable = None
                 , filter_by: str = None
                 , order_by: Column = None
                 , isParent: bool = False
                 , isCombined: bool = False
                 ):
        """

        Declare a custom yser shaped resource.

        Args:
            :model_class (DeclarativeMeta | None): model.TableName
            :alias (str, optional): _description_. Defaults to "".
            :fields (list[tuple[Column, str]  |  Column], optional): model.Table.Column. Defaults to [].
            :children (list[CustomEndpoint] | CustomEndpoint, optional): CustomEndpoint(). Defaults to []. (OneToMany)
            join_on: list[tuple[Column, Column]] - this is a tuple of parent/child multiple field joins 
            :calling - name of function (passing row for virtual attributes or modification)
            :filter_by is string object in SQL format (e.g. '"ShipDate" != null')
            :order_by is Column object used to sort aac result (e.g. order_by=models.Customer.Name)
            :isParent = if True - use parent foreign key to join single lookup (ManyToOne)
            :isCombined =  combine the fields of the isParent = routeTrue with the _parentResource (flatten) 
            
        """
        if not model_class:
            raise ValueError("CustomEndpoint model_class=models.EntityName is required")
        #if join_on is None and children is not None or parent is not None:
        #    raise ValueError("join_on= is required if using children (child column)")

        self._model_class = model_class
        self.alias = alias or model_class._s_type.lower()
        self.fields = fields
        self.children = children or []
        self.calling = calling
        self.filter_by = filter_by
        self.order_by = order_by
        self.isCombined = isCombined
        self.join_on = join_on 
        self.isParent= isParent 
    
        # TODO 
        if isinstance(join_on, tuple):
            if len(join_on) > 0:
                # get parent or child
                self.foreignKey = join_on[0] #TODO - if we have multiple joins
        else:
            self.foreignKey = join_on

        # Internal Private 
        self.primaryKey: str = inspect(model_class).primary_key[0].name
        self.primaryKeyType: str = inspect(self._model_class).primary_key[0].type
        self._model_class_name: str = self._model_class._s_class_name
        # inspect(model_class).primary_key[0].type = Integer() ...
        self._parentResource: CustomEndpoint = None  # ROOT
        self._pkeyList: list = [] # primary key list TODO collect when needed - do not store
        self._fkeyList: list = [] # foreign_key list (used by isParent) TODO collect when needed - do not store
        self._dictRows: list = [] # temporary holding for query results (Phase 1)
        self._parentRow = Dict[str, any] # keep track of linkage

    def __str__(self):
            print(
                f"Alias {self.alias} Model: {self._model_class.__name__} PrimaryKey: {self.primaryKey} FilterBy: {self.filter_by} OrderBy: {self.order_by}")

    def get(self: CustomEndpoint, request: safrs.request.SAFRSRequest, include: str, altKey: str = None) -> dict:
        """_summary_

        Args:
            :self (CustomEndpoint): 
            :request (safrs.request.SAFRSRequest): 
            :include (str): name(s) of relationship from swagger include=
            :altKey (str, optional):  Defaults to None.

        Returns:
            dict: JSON result
        """
        #TODO if security header needs Bearer token
        serverURL = f"{request.host_url}api"
        query = f"{serverURL}/{self._model_class_name}?include={include}"
        args = request.args
        key, value = self.parseArgs(args)
        #key = args.get(key) if args.get(key) is not None else args.get(f"filter[{key}]")
        if altKey is not None:
            query += f"&filter%5B{self.primaryKey}%5D={altKey}"
        elif key is not None:
            query += f"&filter%5B{key}%5D={value}"
        limit = args.get("page[limit]") or 10
        offset = args.get("page[offset]") or 0
        params = {'page[limit]': limit, 'page[offset]': offset}
        resource_logger.debug(f"CustomEndpoint get using query: {query}")
        if Config.SECURITY_ENABLED:
            jwt = request.headers.get("Authorization") or ""
            header = {"Authorization": jwt,"Content-Type": "application/json"}
            result = requests.get(query, headers=header, params=params)
        else:
            result = requests.get(query, params=params)
        if result.status_code == 200:
            jsonResult = json.loads(result.content)
            self._populateResponse(jsonResult) # Pass the JSON result to CustomEndpoint 
            result = self.execute(request) # a dict of args
            return result
        return {"error": result.status_code}
        
    def execute(self: CustomEndpoint, request: safrs.request.SAFRSRequest, altKey: str = None) -> dict:
        """
        execute a model_class resource 
        Args:
            :self (CustomEndpoint): 
            : request SarfsRequest (holds args and data payload, etc)
            : altKey optional <key> passed from @app.route("/foo/<altKey>")
            
            'page[offset]=0 or offset=0
            'page[limit]=10 or limit=10
            'sort=CompanyName'
            'filter[id]=ALFKI or Id=ALFKI
            
        #self._model_class.get_instance("ALFKI") returns the Cusomter/OrderList/OrderDetailList and Product
        #util.row_to_dict(self._model_class.get_instance("ALFKI").OrderList[0].OrderDetailList[0].Product)
        #for f in util.row_to_dict(self._model_class.get_instance("ALFKI")).get("relationships") :print(f'{util.row_to_dict(self._model_class.get_instance("ALFKI"))}.{f}')
        #self._model_class.get_instance('ALFKI')._s_relationships.get("OrderList").synchronize_pairs
        #"Manager" in self._model_class._s_relationships.keys() #_s_jsonapi_attrs.keys()
        #rom .jsonapi_formatting import jsonapi_filter_query, jsonapi_filter_list, jsonapi_sort, jsonapi_format_response, paginate
        #self._model_class.__mapper__.relationships.get("Manages").primaryjoin.left or right left.key or right.key
      
        '''

        Returns:
            dict: data dict from sql
        """
        args = {}
        payload = {}
        result = {}
        jwt = ""
        if request is not None:
            jwt = request.headers.get("Authorization") or ""
            method = request.method
            args = request.args
            self._printIncludes(1) # debug print
            if method == 'DELETE':
                raise ValidationError( 'Delete is not supported at this time')
            elif method == 'OPTION':
                return result
            elif method in ["POST","PUT","PATCH"]:
                try:
                    payload = json.loads(request.data.decode('utf-8'))
                    api = "api" # TODO Config.API_PREFIX.replace("/","")  
                    serverURL = f"{request.host_url}{api}"
                    url = f"{serverURL}/{self._model_class_name}"
                    return self.handlePayload(method, payload, url, jwt)
                except Exception as ex:
                    raise ValidationError( f'{method} error on entity {self._model_class_name} msg: {ex}') from ex
            
        resource_logger.debug(f"CustomEndpoint execute on: {self._model_class_name} using alias: {self.alias}")
        filter_by = None
        #key = args.get(pkey) if args.get(pkey) is not None else args.get(f"filter[{pkey}]")
        pkey , value = self.parseArgs(args)
        if value is not None:
            filter_by = f'{pkey} = {self.quoteStr(value)}'
            self._pkeyList.append(self.quoteStr(value))
        elif altKey is not None:
            filter_by = f'{pkey} = {self.quoteStr(altKey)}'
            self._pkeyList.append(self.quoteStr(altKey))
        limit = args.get("page[limit]") or 20
        offset = args.get("page[offset]") or 0
        order_by = args.get("sort")
       
        try:
            self._createRows(limit=limit,offset=offset,order_by=order_by,filter_by=filter_by) 
            self._executeChildren()
            self._modifyRows(result)
            return json.dumps(result)
        except Exception as ex:
            resource_logger.error(f"CustomEndpoint error {ex}")
            return f"'error': {ex}"

    def _executeChildren(self):
        """
        Recursive execution of included CustomEndpoints
        """
        if isinstance(self.children, CustomEndpoint):
            self.children._parentResource = self
            self.children._processChildren()
        elif len(self.children) > 0:
            for child in self.children:
                child._parentResource = self
                child._processChildren()
        
    def _collectPKeys(self, keyName)-> list:
        keyList = []
        if keyName is None:
            return keyList
        for row in self._dictRows:
            if keyName in row:
                key = row.get(keyName)
                if key is not None and key not in keyList:
                    keyList.append(key)

        self._pkeyList = keyList
        return keyList
        
    def _collectParentKeys(self, keyName: str) -> list:
        keyList = []
        #if not self.isParent or keyName is None:
        if self._parentResource is None or keyName is None:
            return self._parentResource._pkeyList
        for row in self._parentResource._dictRows:
            if keyName in row:
                key = row.get(keyName)
                if key is not None and key not in keyList:
                    keyList.append(key)

        self._fkeyList = keyList
        return keyList

    def _createRows(self,limit:int = 10, offset = 0, filter_by: str = None, order_by: str = None):
        """
        execute and store rows based on list of keys in model
        :limit = 10
        :offset = 0
        :filter_by root only
        :order_by root only
        """
        # If _populateResponse is used - the _dictRows are already filled
        # or the parent resource has now rows - so no need to fetch
        if len(self._dictRows) > 0 or \
            (self._parentResource is not None \
            and len(self._parentResource._dictRows) == 0):
            return
        model_class = self._model_class
        model_class_name = self._model_class_name
        queryFilter = self._createFilterFromKeys()
        session_qry= session.query(model_class)
        #t = session.execute(select(model_class).where(text("Id = 'ALFKI'"))).all() #.join(models.Customer.OrderList)).order()
        if queryFilter is None or queryFilter == 'None':
            #query = select(self._model_class)
            resource_logger.debug(
                    f"CreateRows on {model_class_name} using filter_by: {self.filter_by} order_by: {self.order_by}")
            if self.filter_by is not None:
                qry = session_qry.filter(self.filter_by)
                if self.order_by is not None:
                    qry = qry.order_by(self.order_by)
                if filter_by is not None:
                    resource_logger.debug(
                    f"Adding filter_by: {filter_by}")
                    qry = qry.filter(text(filter_by))
                rows = qry.limit(limit).offset(offset).all()
            else:
                if filter_by is not None:
                    resource_logger.debug(
                    f"Adding filter_by: {filter_by}")
                    qry = session_qry.filter(text(filter_by))
                    rows = qry.limit(limit).offset(offset).all()
                else:
                    rows = session_qry.limit(limit).offset(offset).all()
        else:
            resource_logger.debug(
                f"CreateRows on {model_class_name} using QueryFilter: {queryFilter} order_by: {self.order_by}")
            if self.order_by is not None:
                qry = session_qry.filter(text(queryFilter)).order_by(self.order_by)
            elif  self.filter_by is None:
                qry = session_qry.filter(text(queryFilter))
            else:
                if filter_by:
                    resource_logger.debug(
                    f"Adding on {model_class_name} using filter_by: {filter_by}")
                    qry = session_qry.filter(text(filter_by))#.filter(text(self.filter_by))
            rows = qry.limit(limit).offset(offset).all()
        if rows:    
            dictRows = self.rows_to_dict(rows)
            self._dictRows = dictRows
        

    def _createFilterFromKeys(self):
        aFilter = None
        if self.join_on:
            """
            join_on=[(models.SourceDatum.clientId, models.SourceDatum.clientId),(models.SourceDatum.dataYear, models.SourceDatum.priorYear)]
            we may have multiple joins - need to collect each one 
            #clientId in (clientId keys) and  dataYear in (priorYear keys)
            #result += F"{and} " + self._extractedFromKeys(keyName, keys)
            
        """     
        if isinstance(self.join_on, list):
            andOp = ""
            for join in self.join_on:
                aFilter = self.buildJoin(andOp, join)
                andOp = " and "
        else:
            aFilter = self.buildJoin("", self.join_on)        
       
        return aFilter

    def buildJoin(self, andOp: str, join: Column) -> str:
        joinStr = None
        if join is not None:
            if join.__class__.__name__ == 'InstrumentedAttribute':
                if hasattr(join,"prop") and join.prop.__class__.__name__ == 'RelationshipProperty':
                #    pass #oin.prop._join_condition.foreign_key_columns
                    for l in join.prop._join_condition.foreign_key_columns: 
                        fkeyName = self.primaryKey if self.isParent else l.key
                        self.foreignKey = l
                        keyName = l.key if self.isParent else self.primaryKey
                else:
                    fkeyName = self.primaryKey if self.isParent else join.key #child - parent pkey is implied
                    keyName = join.key if self.isParent else self.primaryKey
                
            elif len(join) == 2:
                pkeyName = join[0].key #parent
                fkeyName = join[1].key #child
                self.primaryKey = fkeyName if self.isParent else self.primaryKey
                self.foreignKey = join[1] if self.isParent else join[0]
                keyName = join[1].key if self.isParent else pkeyName
            
            keys = self._collectParentKeys(keyName)
            if keys is not None:
                joinStrKeys = self._extractedFromKeys(fkeyName , keys)
            return f"{andOp}{joinStrKeys}"
        return None
            

    def _extractedFromKeys(self, keyName: str, keys: object):
        if keys is None or len(keys) == 0:
            return None
        keys = f"{keys}"
        keys = keys.replace("[", "")
        keys = keys.replace("]", "")
        result = ""
        if len(keys) > 0:
            result = f'{keyName} in ({keys})'
            result = result.replace(".", "\".\"")
        if self.filter_by is not None:
            result += f" and text({self.filter_by})"
        return result

    def _printIncludes(self, level: int):
        parenName = self._parentResource._model_class_name if self._parentResource is not None else "None"
        if self.foreignKey:
            print(
                level * ' ', f"CustomEndpoint alias: {self.alias} model: {self._model_class.__name__} primaryKey: {self.primaryKey} join_on: {self.foreignKey} parent: {parenName}")
        else:
            print(
                level * ' ', f"CustomEndpoint alias: {self.alias} model: {self._model_class.__name__} primaryKey: {self.primaryKey} parent: {parenName}")
        if isinstance(self.fields, tuple) and len(self.fields) > 0:
            fields = self.getPrintableFields()
            print(level * '  ', f"Fields: {fields}", sep=", ")
        elif isinstance(self.fields, sqlalchemy.orm.attributes.InstrumentedAttribute):
            print(level * '  ', f"Fields: {self.fields.key}", sep=", ")
        if isinstance(self.children, CustomEndpoint):
            self.children._parentResource = self
            self.children._printIncludes(level + 1)
        elif len(self.children) > 0:
            for incl in self.children:
                incl._parentResource = self
                incl._printIncludes(level + 1)

    def getPrintableFields(self):
        result = ""
        if len(self.fields) > 0:
            for fld in self.fields:
                if isinstance(fld, str):
                    result += f" alias: {fld} "
                elif isinstance(self.fields, sqlalchemy.orm.attributes.InstrumentedAttribute):
                    result += f" {fld[0].key} " if isinstance(fld, tuple) else f" {fld} "
        return result
    
    def _modifyRows(self, result):
        """
        Start at root row and descend to each child
        Args:
            result dict modified and shaped JSON
        """
        result[self.alias] = []
        for row in self._dictRows:
            newRow = self._modifyRow(row)
            result[self.alias].append(newRow)
            #need to link each newRow with one or more childRows
            if isinstance(self.children, CustomEndpoint):
                self.children._linkAndModifyRows(row, newRow)
            elif len(self.children) > 0:
                for child in self.children:
                    child._linkAndModifyRows(row, newRow)

    def _linkAndModifyRows(self, row: dict, modifiedRow: dict):
        """
            link rows to parent 
        Args:
            row (dict): this is the parent row
            modifiedRow (dict): this is the same row that has been modified
        """
        if not self.isCombined:
            modifiedRow[self.alias] = []
        self._parentRow  = DotDict(row)
        pkeyValue = row[self.foreignKey.key] if self.isParent and self.foreignKey.key in row else row[self.primaryKey]
        fkey = self.primaryKey  if self.isParent and self.primaryKey in row else self.foreignKey.key if self.foreignKey is not None else None
        for dictRow in self._dictRows:
            if fkey is not None and f"{pkeyValue}" == f"{dictRow[fkey]}":
                newRow = self._modifyRow(dictRow)
                if self.isParent and self.isCombined:
                    modifiedRow |= newRow
                else:
                    modifiedRow[self.alias].append(newRow)
                if isinstance(self.children, CustomEndpoint):
                    self.children._linkAndModifyRows(dictRow, newRow)
                elif len(self.children) > 0:
                    for include in self.children:
                        include._linkAndModifyRows(dictRow, newRow)

    def _modifyRow(self, dict_row: dict) -> dict:
        #row = self.transform('LAC','',dict_row)
        newRow = DotDict({})
        tableRow = DotDict(dict_row)
        if isinstance(self.fields, sqlalchemy.orm.attributes.InstrumentedAttribute):
            f = self.fields
            fieldName = f[0].key if isinstance(f, tuple) else f.key
            alias = f[1] if isinstance(f, tuple) else fieldName
            if fieldName in tableRow:
                newRow[alias] = tableRow[fieldName]
        elif len(self.fields) > 0:
            for f in self.fields:
                if isinstance(f,str):
                    fieldName = f
                    alias = fieldName
                else:
                    fieldName = f[0].key if isinstance(f, tuple) else f.key
                    alias = f[1] if isinstance(f, tuple) else fieldName
                if fieldName in tableRow:
                    newRow[alias] = tableRow[fieldName]
        else:
            newRow = tableRow
        # allow adding or changes using defined function
        if self.calling is not None:
            try:
                resource_logger.debug(f"calling function {self.calling}")
                self.calling(newRow, tableRow, self._parentRow)
            except Exception as ex:
                resource_logger.error(f"unable to execute fn {self.calling} error: {ex}")
        if not self.isCombined:
            self.insertCheckSum(newRow, tableRow)
            
        return newRow
    
    def insertCheckSum(self, newRow: dict, tableRow: dict):
        if Config.OPT_LOCKING == "required" \
            and ("S_CheckSum" not in newRow and "S_CheckSum" in tableRow):
            newRow["S_CheckSum"] = tableRow.S_CheckSum
            newRow = self.move_checksum(newRow)
        elif "@metadata" in tableRow:
            newRow["@metadata"] = tableRow["@metadata"]
            if "S_CheckSum" in newRow:
                newRow.pop("S_CheckSum")
            
    
    def addRowToResult(self, result: any, rows: any):
        """_summary_

        Args:
            result (any): this is the final output
            rows (any): the unmodified dict rows
        """
        if self.foreignKey is None:
            return
        fkey = self.foreignKey.key if isinstance(self.foreignKey, object) else self.foreignKey
        resource_logger.debug(f"Add Row to Result {self._model_class_name} using {fkey}")
        keyList =  self._fkeyList if self.isParent else self._parentResource._pkeyList 
        for parentKey in keyList:
            for r in result:
                r[self.alias] = []
                for row in rows:
                    fkeyValue = row.get(fkey)
                    if parentKey == fkeyValue:
                        #modifiedRow = self._modifyRow(row)
                        r[self.alias].append(row)
                        
    def _populateResponse(self, jsonResponse):
        """
        Given a json response extract and populate internal dictRows
        Args:
            jsonResponse (_type_): _description_
        """
        #assume 1 data row
        jsonDict = DotDict(jsonResponse)
        if len(jsonDict.data) == 0:
            return
        for data in jsonDict.data:
            key =  data["id"] 
            row = data["attributes"]
            if self.primaryKey == "id" and "id" not in row:
                row["id"] = key #this is a hack since id is a jsonapi reserved value
            self._dictRows.append(row) 
            if key not in self._pkeyList:
                self._pkeyList.append(key)
            model_type = data["type"]
            resource_logger.debug(f"_populateResponse row class on {self._model_class_name} using model_type: {model_type} with key {key}")
        if self.children is not None:
            included = jsonDict.included
            if len(included) == 0:
                return
            if isinstance(self.children, list):
                for child in self.children:
                    child._parentResource = self
                    child.processIncludedRows(included)
            else:
                self.children._parentResource = self
                self.children.processIncludedRows(included)

    def processIncludedRows(self, included: list):
        for parentKey in self._parentResource._pkeyList:
            for row in included:
                model_class_name = row["type"]
                if model_class_name == self._model_class_name:
                    resource_logger.debug(f"includeRow for {self._model_class_name}")
                    attrRow = row["attributes"]
                    if "id" not in "attrs" and "id" in row:
                        attrRow["id"] = row["id"]
                    keyName = self.primaryKey if self.isParent else self.join_on.key
                    if keyName in attrRow and parentKey == attrRow[keyName]:
                        resource_logger.debug(f"includeRow for {self._model_class_name} checking {model_class_name} using Key: {keyName} ")
                        #links = row["links"]
                        #relns = row["relationships"]
                        self._dictRows.append(attrRow)
                        key = attrRow[self.primaryKey]
                        if key not in self._pkeyList:
                            self._pkeyList.append(key)
        if self.children is not None:
            if isinstance(self.children, list):
                for child in self.children:
                    child._parentResource = self
                    child.processIncludedRows(included)
            else:
                self.children._parentResource = self
                self.children.processIncludedRows(included)
                
    def handlePayload(self, method: str, payload: any, url: str, jwt: str) -> any:
        """ tests
            stmt = ""
            if method == 'POST':
                stmt = insert(self._model_class).values(payload)
            #elif  stmt = update(self._model_class).values(payload) #.where(self.primaryKey = 1)
            clz = self._model_class
            key = self.populateClass(clz, payload)
            db.session.add(clz)
            #db.engine.execute(stmt)
            # db.session.select().filter_by().one()
            return db.engine.execute(f"select * from {self._model_class_name} limit 1").one()
        """
        j = self.create_args(payload)
        #TODO check payload for a single row
        clz = self._model_class
        #key = self.populateClass(clz, payload)
        key = payload[self.primaryKey] if self.primaryKey in payload else "-1"
        if Config.SECURITY_ENABLED:
            header = {"Authorization": jwt,"Content-Type": "application/json","accept": "application/vnd.api+json"}
            response = (
                requests.post(url=url, json=j, headers=header)
                if method == 'POST'
                    else requests.patch(url=f"{url}/{key}", json=j, headers=header)
            )
        else:
            response = (
                requests.post(url=url, json=j) 
                if method == "POST" 
                    else requests.patch(url=f"{url}/{key}", data=j) 
                )
        return json.dumps(json.loads(response.text)["data"]["attributes"]) if response.status_code < 301 else response.content

    def populateClass(self, clz, payload):
        for p in payload:
            clz(p = payload[p])
        return clz[self.primaryKey]
    
    def create_args(self, attributes):
        key = attributes[self.primaryKey] if self.primaryKey in attributes else None
        result = None
        if key is None:
            result =  \
                { "data": {
                    "attributes": attributes,
                    "type": self._model_class_name
                }
            }
        else:
            result = \
                { "data": {
                    "attributes": self.move_metadata(attributes),
                    "type": self._model_class_name,
                    "id": key
                }
            }
        v =  str(result)
        v = v.replace("'","\"",1000)
        return json.loads(v.replace("None","null",100))

    def move_metadata(self, json_dict:dict) -> dict:
        if "@metadata" in json_dict:
            json_dict["S_CheckSum"] = json_dict["@metadata"]["checksum"]
            json_dict.pop("@metadata")
        return json_dict
         
    def quoteStr(self, val):
        return val if f"{self.primaryKeyType}" == 'INTEGER' else f"'{val}'"
    
    def rows_to_dict(self: CustomEndpoint, result: flask_sqlalchemy.BaseQuery) -> list:
        """
        Converts SQLAlchemy result to dict array

        Args:
            result (object): SQLAlchemy result

        Returns:
            dict: dict array
        """
        rows = []
        for each_row in result:
            row_as_dict = None
            print(f'type(each_row): {type(each_row)}')
            if isinstance (each_row, sqlalchemy.engine.row.Row):  # sqlalchemy.engine.row
                row_as_dict = each_row._asdict()
            else:
                row_as_dict = each_row.to_dict()
            if not hasattr(row_as_dict,"id"):
                with contextlib.suppress(Exception):
                    row_as_dict["id"] = each_row.id
            rows.append(row_as_dict)
        return rows

    def row_to_dict(self: CustomEndpoint, row
                , replace_attribute_tag: str = ""
                , remove_links_relationships: bool = False) -> dict:
        """
        returns dict suitable for safrs response

        Args:
            row (safrs.DB.Model): a SQLAlchemy row
            replace_attribute_tag (str): replace _attribute_ tag with this name
            remove_links_relationships (bool): remove these tags
        Returns:
            _type_: dict (suitable for flask response)
        """
        row_as_dict = jsonify(row).json
        resource_logger.debug(f'Row: {row_as_dict}')
        if replace_attribute_tag != "":
            row_as_dict[replace_attribute_tag] = row_as_dict.pop('attributes')
        if remove_links_relationships:
            row_as_dict.pop('links')
            row_as_dict.pop('relationships')
        if not hasattr(row_as_dict,"id"):
            with contextlib.suppress(Exception):
                row_as_dict["id"] = row["id"] 
        return row_as_dict

    def _processChildren(self):
        resource_logger.debug(
                f"_executeChildren a child: {self._model_class_name} isParent: {self.isParent}")
        self._createRows()
        self._executeChildren()
        
    def parseArgs(self,args):
        pkey = self.primaryKey
        value = args.get(pkey) if args.get(pkey) is not None else args.get(f"filter[{pkey}]")
        if value is None:
            _sys_filter:str = args.get("sysfilter") 
            _filter:str = args.get("filter")
            """
            sysfilter=equal(fieldName, value)
            filter=fieldName=value
            
            """
            if _sys_filter:
                 if _sys_filter.startswith("equal("):
                    f = _sys_filter[6:-1].split(",")
                    pkey = f[0]
                    value = f[1]
            elif _filter:
                f = _filter.split("=")
                pkey = f[0]
                value = f[1]
        
        return pkey, value
    
    def transform(self, style:str, key:str, json_: dict) -> dict:
	    # use this to change the output (pipeline) of the result
        json_dict = {}
        try:
            json_dict = json.loads(json_) if isinstance(json_, str) else json_
        except Exception as ex:
            resource_logger.error(f"Transform Error on style {style} using key: {key} error: {ex}")
            return json_

        json_result = json_dict.get(key, json_dict) if key in json_dict else [json_dict]
        if isinstance(json_result,list):
            newRes = []
            for row in json_result:
                r = self.move_checksum(row)
                newRes.append(r)
            return newRes
        return self.move_checksum(json_result)
    

    def move_checksum(self, json_dict:dict) -> dict:
        if "S_CheckSum" in json_dict:
            checksum = json_dict["S_CheckSum"]
            json_dict["@metadata"] = { "checksum" : checksum}
            json_dict.pop("S_CheckSum")
        if "_check_sum_" in json_dict:
            json_dict.pop("_check_sum_")
        return json_dict