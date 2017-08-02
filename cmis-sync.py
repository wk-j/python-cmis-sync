#!/usr/bin/env python
from cmislib.model import CmisClient, Document
from cmislib.exceptions import ObjectNotFoundException, CmisException
from time import sleep
import sys
import os
import pickle

import settings
import mapping

SAVE_FILE = 'lastSync.p'


def main():
    while True:
        sync()
        print "Polling for changes every %d seconds" % settings.POLL_INTERVAL
        print "Use ctrl+c to quit"
        print "Sleeping..."
        sleep(settings.POLL_INTERVAL)


def sync():
    # Connect to the source repo
    sourceClient = CmisClient(settings.SOURCE_REPOSITORY_URL,
                          settings.SOURCE_USERNAME,
                          settings.SOURCE_PASSWORD)
    sourceRepo = sourceClient.defaultRepository
    dumpRepoHeader(sourceRepo, "SOURCE")

    # Make sure it supports changes, bail if it does not
    if sourceRepo.getCapabilities()['Changes'] == None:
        print "Source repository does not support changes:" + sourceRepo.getCapabilities()['Changes']
        sys.exit(-1)
    latestChangeToken = sourceRepo.info['latestChangeLogToken']
    print "Latest change token: %s" % latestChangeToken

    # Connect to the target repo
    targetClient = CmisClient(settings.TARGET_REPOSITORY_URL,
                          settings.TARGET_USERNAME,
                          settings.TARGET_PASSWORD)
    targetRepo = targetClient.defaultRepository
    dumpRepoHeader(targetRepo, "TARGET")
    print "    Path: %s" % settings.TARGET_ROOT
    
    # Get last token synced from savefile
    # Using the repository IDs so that you can use this script against
    # multiple source-target pairs and it will remember where you are
    syncKey = "%s><%s" % (sourceRepo.id, targetRepo.id)
    lastChangeSynced = {}
    changeToken = None

    if (os.path.exists(SAVE_FILE)):
        lastChangeSynced = pickle.load(open(SAVE_FILE, "rb" ))
        if lastChangeSynced.has_key(syncKey):
            print "Last change synced: %s" % lastChangeSynced[syncKey]
            changeToken = lastChangeSynced[syncKey]
        else:
            print "First sync..."
    else:
        print "First sync..."

    if changeToken == latestChangeToken:
        print "No changes since last sync so no work to do"
        return

    # Ask the source repo for changes
    changes = None
    if changeToken != None:
        changes = sourceRepo.getContentChanges(changeLogToken=changeToken)
    else:
        changes = sourceRepo.getContentChanges()

    # Process each change
    for change in changes:
        if change.changeType == 'created' or change.changeType == 'updated':
            processChange(change, sourceRepo, targetRepo)

    lastChangeSynced[syncKey] = latestChangeToken
    pickle.dump(lastChangeSynced, open(SAVE_FILE, "wb"))
    return


def processChange(change, sourceRepo, targetRepo):
    """
    Processes a given change by replicating the change from the source
    to the target repository.
    """

    print "Processing: %s" % change.objectId

    # Grab the object
    sourceObj = None
    try:
        sourceObj = sourceRepo.getObject(change.objectId,
                                         getAllowableActions=True)
    except ObjectNotFoundException:
        print "Warning: Change log included an object that no longer exists"
        return

    if (sourceObj.properties['cmis:objectTypeId'] != 'cmis:document' and
        sourceObj.properties['cmis:objectTypeId'] != 'cmis:folder' and
        not(mapping.mapping.has_key(sourceObj.properties['cmis:objectTypeId']))):
        return

    sourcePath = sourceObj.getPaths()[0]  # Just deal with one path for now
    print "Source Path: %s" % sourcePath
    targetPath = settings.TARGET_ROOT + sourcePath

    sourceProps = sourceObj.properties

    # Determine if the object exists in the target
    targetObj = None
    try:
        targetObj = targetRepo.getObjectByPath(targetPath)

        if targetObj is Document :
            targetObj = targetObj.getLatestVersion()
            print "Version label:%s" % targetObj.properties['cmis:versionLabel']
        
        # If it does, update its properties
        props = getProperties(targetRepo, sourceProps, 'update')
        if (len(props) > 0):
            print props
            targetObj = targetObj.updateProperties(props)

    except ObjectNotFoundException:
        print "Object does not exist in TARGET"
        props = getProperties(targetRepo, sourceProps, 'create')        
        targetObj = createNewObject(targetRepo, targetPath, props)
        if targetObj == None:
            return
        
    # Then, update its content if that is possible
    #targetObj.reload()
    if sourceObj is Document :

        if (sourceObj.allowableActions['canGetContentStream'] == True and
            targetObj.allowableActions['canCheckOut'] == True):
            print "Updating content stream in target object version:%s" % targetObj.properties['cmis:versionLabel']
            #print "target props:%s" % targetObj.properties['cmisbook:copyright']
            pwc = targetObj.checkout()
            pwc.setContentStream(
                sourceObj.getContentStream(),
                contentType=sourceObj.properties['cmis:contentStreamMimeType'])
            pwc.checkin(major=False)
            print "Checkin is done, version:%s" % targetObj.properties['cmis:versionLabel']
            targetObj.reload()
        
            #print "target props:%s" % targetObj.properties['cmisbook:copyright']
        
def getProperties(targetRepo, sourceProps, mode):
    sourceTypeId = sourceProps['cmis:objectTypeId']
    props = {}

    if mode == 'create':
        props['cmis:name'] = sourceProps['cmis:name']
        props['cmis:objectTypeId'] = sourceTypeId            

    # if the source type is cmis:document, don't move any custom properties
    # set the type and return
    if sourceTypeId == 'cmis:document' or sourceTypeId == 'cmis:folder':
        return props

    # otherwise, get the target object type from the mapping
    targetObjectId = mapping.mapping[sourceTypeId]['targetType']
    if mode == 'create':
        props['cmis:objectTypeId'] = targetObjectId
    print "Target object id: %s" % targetObjectId

    targetTypeDef = targetRepo.getTypeDefinition(targetObjectId)
    
    # get all of the target properties
    for propKey in mapping.mapping[sourceTypeId]['properties'].keys():
        targetPropId = mapping.mapping[sourceTypeId]['properties'][propKey]
        if sourceProps[propKey] != None:
            if targetTypeDef.properties[targetPropId].getUpdatability() == 'readwrite':
                props[targetPropId] = sourceProps[propKey]
                print "target prop: %s" % targetPropId
                print "target val: %s" % sourceProps[propKey]
            else:
                print "Warning, target property changed but isn't writable in target:%s" % targetPropId
        
    return props

def createNewObject(targetRepo, path, props):
    """
    Creates a new object given a target repo, the full path of the
    object, and a bundle of properties. If any elements of the
    specified path do not already exist as folders, they are created.
    """

    print "Creating new object in: %s" % path
    parentPath = '/'.join(path.split('/')[0:-1])

    # determine base type
    typeDef = targetRepo.getTypeDefinition(props['cmis:objectTypeId'])

    parentFolder = getParentFolder(targetRepo, parentPath)
    targetObj = None
    if (typeDef.baseId == 'cmis:document'):
        try:
            targetObj = parentFolder.createDocumentFromString(
                props['cmis:name'],
                props,
                contentString='')
        except CmisException:
            print "ERROR: Exception creating object"
            return None
    else:
        targetObj = parentFolder.createFolder(props['cmis:name'], props)
    return targetObj


def getParentFolder(targetRepo, parentPath):
    """
    Gets the folder at the parent path specified, or creates it if it
    does not exists. Recursively calls createNewObject so that the
    folders in the entire path are created if necessary.
    """

    print "Getting parent folder: %s" % parentPath
    if parentPath == '':
        return targetRepo.rootFolder
    parentFolder = None
    pathList = parentPath.split('/')
    try:
        parentFolder = targetRepo.getObjectByPath(parentPath)
    except ObjectNotFoundException:
        props = {'cmis:name': pathList[-1],
             'cmis:objectTypeId': 'cmis:folder'}        
        parentFolder = createNewObject(targetRepo, parentPath, props)
    return parentFolder


def dumpRepoHeader(repo, label):
    print "=================================="
    print "%s repository info:" % label
    print "----------------------------------"
    print "    Name: %s" % repo.name
    print "      Id: %s" % repo.id
    print "  Vendor: %s" % repo.info['vendorName']
    print " Version: %s" % repo.info['productVersion']


def setLastSync(changeToken):
    sourceClient = CmisClient(settings.SOURCE_REPOSITORY_URL,
                          settings.SOURCE_USERNAME,
                          settings.SOURCE_PASSWORD)
    sourceRepo = sourceClient.defaultRepository

    targetClient = CmisClient(settings.TARGET_REPOSITORY_URL,
                          settings.TARGET_USERNAME,
                          settings.TARGET_PASSWORD)
    targetRepo = targetClient.defaultRepository

    syncKey = "%s><%s" % (sourceRepo.id, targetRepo.id)
    lastChangeSynced = {syncKey: changeToken}
    pickle.dump(lastChangeSynced, open(SAVE_FILE, "wb"))
    print "Forced last sync to: %s" % changeToken

if __name__ == "__main__":
    if len(sys.argv) > 1:
        setLastSync(sys.argv[1])
    main()
