/* Atomic draft-and-publish schematic restore for the oa-git bridge. */

procedure(oagitRestoreString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitRestoreMaybeString(value)
  if(value
      then oagitRestoreString(value)
      else list('jsonNull))
)

procedure(oagitRestoreScalar(value)
  cond(
    (!value list('jsonNull))
    (eq(value t) list('jsonBool t))
    (stringp(value) value)
    (integerp(value) value)
    (floatp(value) value)
    (t list('jsonNull))
  )
)

procedure(oagitRestoreBool(value)
  list('jsonBool if(eq(value t) t nil))
)

procedure(oagitRestoreSetFailure(code message)
  unless(oagitRestoreErrorCode
    oagitRestoreErrorCode = code
    oagitRestoreErrorMessage = message
  )
  nil
)

procedure(oagitRestoreFailureObject()
  if(oagitRestoreErrorCode
      then oagitRestoreErrorMessage
      else list('jsonNull))
)

procedure(oagitRestoreWriteResponse(req ok published schErrors schWarnings
                                    cdfVerified mismatches)
  let((target lib cell view draft derivedRefresh derivedStatus)
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft = req->draft_cell
    derivedRefresh = if(req->recompute_derived
                         then cons('jsonObject reverse(oagitRestoreDerivedRefresh))
                         else list('jsonNull))
    derivedStatus = if(ok && req->recompute_derived
                        then "valid"
                        else list('jsonNull))
    errset(
      oagitWriteJsonFile(
        req->output_path
        list('jsonObject
             list("ok" oagitRestoreBool(ok))
             list("error" if(ok list('jsonNull)
                                  oagitRestoreFailureObject()))
             list("error_code" if(ok list('jsonNull)
                                    if(oagitRestoreErrorCode
                                        oagitRestoreErrorCode
                                        list('jsonNull))))
             list("target"
                  list('jsonObject
                       list("library" oagitRestoreString(lib))
                       list("cell" oagitRestoreString(cell))
                       list("view" oagitRestoreString(view))))
             list("published" oagitRestoreBool(published))
             list("draft_cell" oagitRestoreString(draft))
             list("sch_check"
                  list('jsonObject
                       list("errors" oagitRestoreScalar(schErrors))
                       list("warnings" oagitRestoreScalar(schWarnings))))
             list("cdf_verified" oagitRestoreBool(cdfVerified))
             list("cdf_passes" oagitRestoreScalar(oagitRestoreCdfPasses))
             list("callback_order"
                  cons('jsonObject reverse(oagitRestoreCallbackOrder)))
             list("derived_status" derivedStatus)
             list("derived_refresh" derivedRefresh)
             list("connectivity_mismatches"
                  cons('jsonArray mismatches)))
      )
      t
    )
  )
)

procedure(oagitRestoreFail(req code message)
  oagitRestoreSetFailure(code message)
  oagitRestoreWriteResponse(req nil nil 0 0 nil nil)
  nil
)

procedure(oagitRestoreOpenMaster(lib cell view)
  let((master openedByScript)
    master = dbFindOpenCellViewByName(lib cell view)
    if(master
        then openedByScript = nil
        else
          master = dbOpenCellViewByType(lib cell view nil "r")
          openedByScript = t)
    list(master openedByScript)
  )
)

procedure(oagitRestoreCloseMaster(master openedByScript)
  when(master && openedByScript
    errset(dbClose(master) t)
  )
)

procedure(oagitRestoreParamValue(value paramType)
  cond(
    (equal(paramType "string") oagitRestoreString(value))
    (equal(paramType "cyclic") oagitRestoreString(value))
    (equal(paramType "radio") oagitRestoreString(value))
    (equal(paramType "int")
      if(integerp(value)
          value
          if(numberp(value) fix(value) atoi(oagitRestoreString(value)))))
    (equal(paramType "float")
      if(floatp(value) float(value) cdfParseFloatString(oagitRestoreString(value))))
    (equal(paramType "boolean")
      if(value
          then
            if(equal(value "nil") || equal(value "false") nil t)
          else nil))
    (t nil)
  )
)

procedure(oagitRestorePropValue(value propType)
  cond(
    (equal(propType "string")
      if(stringp(value) value oagitRestoreString(value)))
    (equal(propType "int")
      if(integerp(value)
          value
          if(numberp(value) fix(value) nil)))
    (equal(propType "float")
      if(numberp(value) float(value) nil))
    (equal(propType "boolean")
      if(value t nil))
    (t nil)
  )
)

procedure(oagitRestoreApplyProps(inst props instCdf)
  let((triple pName pValue propType cdfParam converted propResult)
    foreach(triple props
      pName = oagitRestoreString(car(triple))
      pValue = cadr(triple)
      propType = oagitRestoreString(caddr(triple))
      cdfParam = nil
      when(instCdf cdfParam = cdfFindParamByName(instCdf pName))
      unless(member(propType list("string" "int" "float" "boolean"))
        oagitRestoreSetFailure("property_error"
                               "unsupported property type")
      )
      when(!cdfParam && !oagitRestoreErrorCode
        converted = oagitRestorePropValue(pValue propType)
        unless(equal(propType "boolean") || converted != nil
          oagitRestoreSetFailure("property_error"
                                 "invalid property value")
        )
        unless(oagitRestoreErrorCode
          propResult = dbCreateProp(inst pName propType converted)
          unless(propResult
            oagitRestoreSetFailure("property_create_failed"
                                   "dbCreateProp returned nil"))
        )
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCdfValuesEquivP(actual expected)
  let((aNum eNum diff denom)
    cond(
      (equal(actual expected) t)
      (t
        aNum = cond(
          (numberp(actual) float(actual))
          (stringp(actual)
            let((r) r = errset(cdfParseFloatString(actual) t)
              if(r && numberp(car(r)) float(car(r)) nil)))
          (t nil))
        eNum = cond(
          (numberp(expected) float(expected))
          (stringp(expected)
            let((r) r = errset(cdfParseFloatString(expected) t)
              if(r && numberp(car(r)) float(car(r)) nil)))
          (t nil))
        if(aNum && eNum
            then
              diff = abs(aNum - eNum)
              denom = max(abs(aNum) abs(eNum) 1.0e-18)
              diff <= 1.0e-9 * denom
            else nil))
    )
  )
)

procedure(oagitRestoreInvokeCdfHook(procName cdfData)
  let((procStr sym)
    procStr = if(stringp(procName) procName
                 if(symbolp(procName) sprintf(nil "%s" procName) ""))
    if(strlen(procStr) > 0
        then
          if(index(procStr "(")
              then errset(progn(evalstring(procStr) t) t)
              else
                sym = stringToSymbol(procStr)
                if(isCallable(sym)
                    then errset(progn(funcall(sym cdfData) t) t)
                    else nil))
        else list(t))
  )
)

procedure(oagitRestoreIgnoredCdfParamP(pName)
  member(oagitRestoreString(pName) list(
    "lxComponentType"
    "description"
    "pdkVersion"
    "macroArgumentStyle"
    "pasUpdateParamList"
    "lxIgnoredParamsForCAS"
    "modelLabelSet"
    "opPointLabelSet"
    "paramLabelSet"
    "lxStopList"
    "lxUseCell"
  ))
)

procedure(oagitRestoreParamType(param)
  let((pType)
    pType = param~>paramType
    unless(pType pType = param~>type)
    oagitRestoreString(pType)
  )
)

procedure(oagitRestoreScalarP(value paramType)
  cond(
    (equal(paramType "boolean") t)
    (!value nil)
    (stringp(value) t)
    (integerp(value) t)
    (floatp(value) t)
    (t nil)
  )
)

procedure(oagitRestoreCdfValuePresentP(value paramType)
  if(equal(paramType "boolean")
      then t
      else value)
)

procedure(oagitRestoreExportCdfValue(value paramType)
  cond(
    (equal(paramType "boolean") oagitRestoreBool(value))
    (equal(paramType "string") if(stringp(value) value nil))
    (equal(paramType "cyclic") if(stringp(value) value nil))
    (equal(paramType "radio") if(stringp(value) value nil))
    (equal(paramType "int")
      if(numberp(value)
          if(integerp(value) value fix(value))
          nil))
    (equal(paramType "float")
      if(numberp(value) float(value) nil))
    (t nil)
  )
)

procedure(oagitRestoreEditableEvalP(editableExpr inst)
  let((cvType res)
    cond(
      (equal(editableExpr "nil") nil)
      (!editableExpr || eq(editableExpr t) ||
       equal(editableExpr "t") || equal(editableExpr "")
        t)
      (stringp(editableExpr)
        cvType = nil
        when(inst && inst~>cellView
          cvType = oagitRestoreString(inst~>cellView~>cellViewType)
        )
        cond(
          (equal(editableExpr "iPDK_isSchematic()")
            equal(cvType "schematic"))
          (rexMatchp("deGetEditCellView" editableExpr)
            cond(
              (rexMatchp("!=.*\"maskLayout\"" editableExpr) ||
               rexMatchp("==.*\"schematic\"" editableExpr)
                equal(cvType "schematic"))
              (rexMatchp("==.*\"maskLayout\"" editableExpr)
                if(equal(cvType "schematic") nil t))
              (t
                res = errset(evalstring(editableExpr) t)
                if(res && car(res) t nil))
            ))
          (t
            res = errset(evalstring(editableExpr) t)
            if(res && car(res) t nil))
        ))
      (t nil)
    )
  )
)

procedure(oagitRestoreResetInstCdf(instCdf baseCdf)
  let((pName pType baseParam)
    when(instCdf && baseCdf
      foreach(cdfParam instCdf~>parameters
        pName = cdfParam~>name
        pType = oagitRestoreParamType(cdfParam)
        unless(equal(pType "button")
          baseParam = cdfFindParamByName(baseCdf pName)
          when(baseParam
            errset(cdfParam~>value = baseParam~>defValue t)
          )
        )
      )
    )
    t
  )
)

procedure(oagitRestoreOrderParams(record)
  let((paramByName seen ordered orderList triple pName orderName)
    paramByName = makeTable("oagitParamByName" nil)
    seen = makeTable("oagitParamSeen" nil)
    ordered = nil
    foreach(triple record->params
      pName = oagitRestoreString(car(triple))
      paramByName[pName] = triple
    )
    orderList = record->param_order
    when(!orderList && record->cdf_contract && record->cdf_contract->order
      orderList = record->cdf_contract->order
    )
    when(orderList
      foreach(orderName orderList
        pName = oagitRestoreString(orderName)
        when(paramByName[pName] && !seen[pName]
          ordered = cons(paramByName[pName] ordered)
          seen[pName] = t
        )
      )
    )
    foreach(triple record->params
      pName = oagitRestoreString(car(triple))
      unless(seen[pName]
        ordered = cons(triple ordered)
        seen[pName] = t
      )
    )
    reverse(ordered)
  )
)

procedure(oagitRestoreSnapshotEditable(instCdf baseCdf inst)
  let((snap pName pType baseParam editable editableOk)
    snap = nil
    when(instCdf
      foreach(cdfParam instCdf~>parameters
        pName = oagitRestoreString(cdfParam~>name)
        unless(oagitRestoreIgnoredCdfParamP(pName)
          pType = oagitRestoreParamType(cdfParam)
          when(member(pType list("string" "int" "float" "boolean" "cyclic" "radio"))
            baseParam = nil
            when(baseCdf baseParam = cdfFindParamByName(baseCdf pName))
            when(baseParam
              editable = cdfParam~>editable
              editableOk = oagitRestoreEditableEvalP(editable inst)
              when(editableOk && !equal(editable baseParam~>editable)
                editableOk = oagitRestoreEditableEvalP(baseParam~>editable inst)
              )
              when(editableOk
                snap = cons(list(pName cdfParam~>value) snap)
              )
            )
          )
        )
      )
    )
    reverse(snap)
  )
)

procedure(oagitRestoreSnapshotsEquivP(snapA snapB)
  let((tblB same pair k vA vB)
    if(length(snapA) != length(snapB)
        then nil
        else
          tblB = makeTable("oagitSnapB" nil)
          foreach(pair snapB
            tblB[car(pair)] = pair
          )
          same = t
          foreach(pair snapA
            when(same
              k = car(pair)
              vA = cadr(pair)
              if(tblB[k]
                  then
                    vB = cadr(tblB[k])
                    unless(oagitRestoreCdfValuesEquivP(vA vB)
                      same = nil)
                  else same = nil)
            )
          )
          same)
  )
)

procedure(oagitRestoreFindProp(props name)
  let((found)
    found = nil
    foreach(prop props
      when(equal(oagitRestoreString(prop~>name) oagitRestoreString(name))
        found = prop
      )
    )
    found
  )
)

procedure(oagitRestoreCollectDerived(instCdf baseCdf inst requestedTable)
  let((derived pName pType baseParam editable editableOk current defaultValue
       stored differs encodedVal)
    derived = nil
    when(instCdf
      foreach(cdfParam instCdf~>parameters
        pName = oagitRestoreString(cdfParam~>name)
        unless(oagitRestoreIgnoredCdfParamP(pName) ||
               (requestedTable && requestedTable[pName])
          pType = oagitRestoreParamType(cdfParam)
          baseParam = nil
          when(baseCdf baseParam = cdfFindParamByName(baseCdf pName))
          editable = cdfParam~>editable
          editableOk = nil
          when(baseParam &&
               member(pType list("string" "int" "float" "boolean" "cyclic" "radio"))
            editableOk = oagitRestoreEditableEvalP(editable inst)
            when(editableOk && !equal(editable baseParam~>editable)
              editableOk = oagitRestoreEditableEvalP(baseParam~>editable inst)
            )
          )
          current = cdfParam~>value
          defaultValue = nil
          when(baseParam
            defaultValue = baseParam~>defValue
          )
          stored = oagitRestoreFindProp(inst~>prop pName)
          differs = !equal(current defaultValue)
          encodedVal = oagitRestoreExportCdfValue(current pType)
          when(!editableOk && (differs || stored || equal(pName "model")) &&
               oagitRestoreScalarP(current pType) &&
               oagitRestoreCdfValuePresentP(current pType) &&
               encodedVal != nil
            derived = cons(list(pName encodedVal) derived)
          )
        )
      )
    )
    reverse(derived)
  )
)

procedure(oagitRestoreApplyCdf(inst record mLib mCell @optional (req nil))
  let((instCdf baseCdf baseCell orderedParams paramData paramName paramValue
       paramType instParam baseParam editableOk converted convertedSpecs requestedTable
       callbackResult refreshed refreshedParam expectedValue actualValue
       formInit doneProc hookResult maxPasses recomputeDerived pass converged
       instPasses prevEditableSnap beforeSnap afterSnap baselineSnap
       primaryDrifted driftInfo allPrimariesMatch readbackMismatch
       passCallbacks lastExecutedCallbacks spec checkSpec cName cExp cParam
       derivedPairs)
    instCdf = cdfGetInstCDF(inst)
    baseCdf = nil
    baseCell = ddGetObj(mLib mCell)
    when(baseCell baseCdf = cdfGetBaseCellCDF(baseCell))
    maxPasses = 3
    cond(
      (req && req->max_passes && integerp(req->max_passes) && req->max_passes > 0
        maxPasses = req->max_passes)
      (req && req->cdf && req->cdf->max_passes &&
       integerp(req->cdf->max_passes) && req->cdf->max_passes > 0
        maxPasses = req->cdf->max_passes)
    )
    recomputeDerived = if(req && req->recompute_derived t nil)
    /* Bind cdfgData to effective instCdf, leave cdfgForm nil in headless mode
       to avoid CDF-176 errors when PAS formInitProc attaches form state. */
    cdfgData = instCdf
    cdfgForm = nil
    /* Reset ALL instCdf parameters back to baseParam~>defValue before applying
       any requested params to eliminate cross-instance CDF cache leakage. */
    when(instCdf
      oagitRestoreResetInstCdf(instCdf baseCdf)
      formInit = (instCdf && instCdf~>formInitProc) ||
                 (baseCdf && baseCdf~>formInitProc)
      when(formInit && !equal(formInit "")
        hookResult = oagitRestoreInvokeCdfHook(formInit instCdf)
        when(record->params && !hookResult
          oagitRestoreSetFailure(
            "cdf_parameter_error"
            sprintf(nil "CDF formInitProc failed (%L): %L" formInit errset.errset)))
      )
    )
    orderedParams = oagitRestoreOrderParams(record)
    convertedSpecs = nil
    requestedTable = makeTable("oagitRequestedParams" nil)
    foreach(paramData orderedParams
      unless(oagitRestoreErrorCode
        paramName = oagitRestoreString(car(paramData))
        paramValue = cadr(paramData)
        paramType = oagitRestoreString(caddr(paramData))
        instParam = nil
        baseParam = nil
        when(instCdf instParam = cdfFindParamByName(instCdf paramName))
        when(baseCdf baseParam = cdfFindParamByName(baseCdf paramName))
        unless(instParam && baseParam &&
               !equal(instParam~>editable "nil") &&
               !equal(baseParam~>editable "nil")
          oagitRestoreSetFailure("cdf_parameter_error"
                                 "requested parameter is missing or read-only")
        )
        when(!oagitRestoreErrorCode
          cond(
            (equal(paramType "string")
              converted = oagitRestoreParamValue(paramValue paramType))
            (equal(paramType "cyclic")
              converted = oagitRestoreParamValue(paramValue paramType))
            (equal(paramType "radio")
              converted = oagitRestoreParamValue(paramValue paramType))
            (equal(paramType "int")
              converted = oagitRestoreParamValue(paramValue paramType))
            (equal(paramType "float")
              converted = errset(oagitRestoreParamValue(paramValue paramType) t)
              when(converted converted = car(converted)))
            (equal(paramType "boolean")
              converted = oagitRestoreParamValue(paramValue paramType))
            (t
              oagitRestoreSetFailure("cdf_parameter_error"
                                     "unsupported CDF parameter type"))
          )
        )
        when(!oagitRestoreErrorCode
          when(equal(paramType "float")
            unless(numberp(converted)
              oagitRestoreSetFailure("cdf_parameter_error"
                                     "invalid floating-point CDF value")))
          unless(oagitRestoreErrorCode
            convertedSpecs = cons(list(paramName converted paramType) convertedSpecs)
            requestedTable[paramName] = t
          )
        )
      )
    )
    convertedSpecs = reverse(convertedSpecs)
    when(convertedSpecs && !oagitRestoreErrorCode
      pass = 1
      converged = nil
      instPasses = 0
      prevEditableSnap = nil
      lastExecutedCallbacks = nil
      driftInfo = nil
      readbackMismatch = nil
      while(pass <= maxPasses && !converged && !oagitRestoreErrorCode
        instPasses = pass
        cdfgData = instCdf
        cdfgForm = nil
        when(pass > 1 && instCdf
          formInit = (instCdf && instCdf~>formInitProc) ||
                     (baseCdf && baseCdf~>formInitProc)
          when(formInit && !equal(formInit "")
            hookResult = oagitRestoreInvokeCdfHook(formInit instCdf)
            unless(hookResult
              oagitRestoreSetFailure(
                "cdf_parameter_error"
                sprintf(nil "CDF formInitProc failed on pass %d (%L): %L"
                        pass formInit errset.errset)))
          )
        )
        primaryDrifted = nil
        driftInfo = nil
        passCallbacks = nil
        allPrimariesMatch = t
        readbackMismatch = nil
        /* Apply all requested primary values in deterministic order. */
        unless(oagitRestoreErrorCode
          cdfgData = instCdf
          cdfgForm = nil
          foreach(spec convertedSpecs
            instParam = cdfFindParamByName(instCdf car(spec))
            when(instParam
              instParam~>value = cadr(spec)
            )
          )
        )
        when(pass == 1
          /* Validate dynamic editability after requested primary values (including
             mode-switch params such as sd_param="custom" or calcParam_PPE="PreSet")
             have been applied to cdfgData, and before invoking callbacks. */
          foreach(spec convertedSpecs
            unless(oagitRestoreErrorCode
              paramName = car(spec)
              instParam = cdfFindParamByName(instCdf paramName)
              baseParam = nil
              when(baseCdf baseParam = cdfFindParamByName(baseCdf paramName))
              if(instParam && baseParam
                  then
                    editableOk = oagitRestoreEditableEvalP(instParam~>editable inst)
                    when(editableOk && !equal(instParam~>editable baseParam~>editable)
                      editableOk = oagitRestoreEditableEvalP(baseParam~>editable inst)
                    )
                    unless(editableOk
                      oagitRestoreSetFailure(
                        "cdf_parameter_error"
                        sprintf(nil "requested CDF parameter %s is dynamically read-only"
                                paramName)))
                  else
                    oagitRestoreSetFailure(
                      "cdf_parameter_error"
                      sprintf(nil "requested CDF parameter %s is missing" paramName)))
            )
          )
          unless(oagitRestoreErrorCode
            beforeSnap = oagitRestoreSnapshotEditable(instCdf baseCdf inst)
          )
        )
        /* Execute callbacks in order; re-apply requested primaries after each callback. */
        foreach(spec convertedSpecs
          unless(oagitRestoreErrorCode
            paramName = car(spec)
            instParam = cdfFindParamByName(instCdf paramName)
            when(instParam && instParam~>callback && !equal(instParam~>callback "")
              cdfgData = instCdf
              cdfgForm = nil
              /* Cadence PAS (PDK Automation System) consumes/invalidates its
                 headless init buffer when primaries are assigned without a GUI
                 form; re-run PasCdf* formInitProc immediately before each PAS
                 callback so PasCdfCommitValue always sees valid init data. */
              when(formInit && index(oagitRestoreString(formInit) "PasCdf")
                oagitRestoreInvokeCdfHook(formInit instCdf)
              )
              passCallbacks = cons(paramName passCallbacks)
              callbackResult = errset(progn(evalstring(instParam~>callback) t) t)
              unless(callbackResult
                oagitRestoreSetFailure(
                  "cdf_parameter_error"
                  sprintf(nil "CDF callback evaluation failed for %s (%s): %L"
                          paramName instParam~>callback errset.errset)))
              when(!oagitRestoreErrorCode
                foreach(checkSpec convertedSpecs
                  cName = car(checkSpec)
                  cExp = cadr(checkSpec)
                  cParam = cdfFindParamByName(instCdf cName)
                  when(cParam && !oagitRestoreCdfValuesEquivP(cParam~>value cExp)
                    primaryDrifted = t
                    unless(driftInfo
                      driftInfo = list(cName cExp cParam~>value))
                    cParam~>value = cExp
                  )
                )
              )
            )
          )
        )
        unless(oagitRestoreErrorCode
          doneProc = (instCdf && instCdf~>doneProc) ||
                     (baseCdf && baseCdf~>doneProc)
          when(doneProc && !equal(doneProc "")
            hookResult = oagitRestoreInvokeCdfHook(doneProc instCdf)
            unless(hookResult
              oagitRestoreSetFailure(
                "cdf_parameter_error"
                sprintf(nil "CDF doneProc failed (%L): %L" doneProc errset.errset)))
          )
        )
        unless(oagitRestoreErrorCode
          callbackResult = errset(cdfUpdateInstParam(inst) t)
          unless(callbackResult && car(callbackResult)
            oagitRestoreSetFailure("cdf_parameter_error"
                                   "CDF parameter update failed"))
        )
        unless(oagitRestoreErrorCode
          refreshed = cdfGetInstCDF(inst)
          unless(refreshed
            oagitRestoreSetFailure("cdf_parameter_error"
                                   "CDF parameter readback is unavailable"))
          when(refreshed
            foreach(spec convertedSpecs
              unless(oagitRestoreErrorCode
                refreshedParam = cdfFindParamByName(refreshed car(spec))
                unless(refreshedParam
                  oagitRestoreSetFailure("cdf_parameter_error"
                                         "CDF parameter readback is missing"))
                when(refreshedParam
                  expectedValue = cadr(spec)
                  actualValue = refreshedParam~>value
                  unless(oagitRestoreCdfValuesEquivP(actualValue expectedValue)
                    allPrimariesMatch = nil
                    unless(readbackMismatch
                      readbackMismatch = list(car(spec) expectedValue actualValue)))
                )
              )
            )
          )
        )
        unless(oagitRestoreErrorCode
          instCdf = refreshed
          cdfgData = instCdf
          cdfgForm = nil
          afterSnap = oagitRestoreSnapshotEditable(refreshed baseCdf inst)
          baselineSnap = if(pass == 1 beforeSnap prevEditableSnap)
          if(allPrimariesMatch && !primaryDrifted &&
             oagitRestoreSnapshotsEquivP(afterSnap baselineSnap)
              then converged = t
              else prevEditableSnap = afterSnap)
          lastExecutedCallbacks = reverse(passCallbacks)
        )
        pass = pass + 1
      )
      unless(oagitRestoreErrorCode
        oagitRestoreCdfPasses = oagitRestoreCdfPasses + instPasses
        when(lastExecutedCallbacks
          oagitRestoreCallbackOrder = cons(
            list(oagitRestoreString(record->name)
                 cons('jsonArray lastExecutedCallbacks))
            oagitRestoreCallbackOrder)
        )
        if(!converged
            then
              cond(
                (readbackMismatch
                  oagitRestoreSetFailure(
                    "cdf_contradiction"
                    sprintf(nil "CDF parameter contradiction for %s: expected=%L actual=%L"
                            car(readbackMismatch) cadr(readbackMismatch)
                            caddr(readbackMismatch))))
                (driftInfo
                  oagitRestoreSetFailure(
                    "cdf_contradiction"
                    sprintf(nil "CDF parameter contradiction for %s: expected=%L actual=%L"
                            car(driftInfo) cadr(driftInfo) caddr(driftInfo))))
                (t
                  oagitRestoreSetFailure(
                    "cdf_nonconvergent"
                    sprintf(nil "CDF coupled parameters did not converge within %d passes for %s"
                            maxPasses oagitRestoreString(record->name))))
              )
            else
              when(recomputeDerived
                derivedPairs = oagitRestoreCollectDerived(
                  instCdf baseCdf inst requestedTable)
                oagitRestoreDerivedRefresh = cons(
                  list(oagitRestoreString(record->name)
                       cons('jsonObject derivedPairs))
                  oagitRestoreDerivedRefresh)
              ))
      )
    )
    when(!convertedSpecs && recomputeDerived && instCdf && !oagitRestoreErrorCode
      derivedPairs = oagitRestoreCollectDerived(instCdf baseCdf inst requestedTable)
      oagitRestoreDerivedRefresh = cons(
        list(oagitRestoreString(record->name)
             cons('jsonObject derivedPairs))
        oagitRestoreDerivedRefresh)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreApplyProps(inst record->props instCdf)
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCreateInstances(cv records instByName @optional (req nil))
  let((record masterInfo master openedByScript inst)
    foreach(record records
      unless(oagitRestoreErrorCode
        masterInfo = oagitRestoreOpenMaster(record->lib record->cell record->view)
        master = car(masterInfo)
        openedByScript = cadr(masterInfo)
        unless(master
          oagitRestoreSetFailure(
            "master_open_failed"
            sprintf(nil "unable to open instance master %s/%s/%s for %s"
                    oagitRestoreString(record->lib)
                    oagitRestoreString(record->cell)
                    oagitRestoreString(record->view)
                    oagitRestoreString(record->name))))
        when(master && !oagitRestoreErrorCode
          inst = schCreateInst(cv master record->name record->xy record->orient)
          unless(inst
            oagitRestoreSetFailure("instance_create_failed"
                                   "schCreateInst returned nil"))
          when(inst && !oagitRestoreErrorCode
            instByName[record->name] = inst
            oagitRestoreApplyCdf(inst record record->lib record->cell req)
          )
        )
        oagitRestoreCloseMaster(master openedByScript)
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreOrderRecords(records order)
  let((rank out)
    if(order && listp(order)
      then
        rank = makeTable("oagitRestoreOrderRank" nil)
        for(i 0 sub1(length(order))
          rank[nth(i order)] = i)
        out = nil
        foreach(name order
          foreach(record records
            when(equal(oagitRestoreString(record->name)
                       oagitRestoreString(name))
              out = cons(record out))))
        out = reverse(out)
        foreach(record records
          unless(rank[oagitRestoreString(record->name)]
            out = append1(out record)))
        out
      else
        records))
)

procedure(oagitRestoreCreatePins(cv records)
  let((record masterInfo master openedByScript pin netName net term)
    foreach(record records
      unless(oagitRestoreErrorCode
        netName = record->net
        unless(netName && strlen(netName) > 0 netName = record->name)
        masterInfo = oagitRestoreOpenMaster(record->lib record->cell record->view)
        master = car(masterInfo)
        openedByScript = cadr(masterInfo)
        unless(master
          oagitRestoreSetFailure("pin_master_open_failed"
                                 "unable to open pin master"))
        when(master && !oagitRestoreErrorCode
          if(equal(netName record->name)
              then
                pin = schCreatePin(cv master record->name record->direction nil
                                   record->xy record->orient)
                unless(pin
                  oagitRestoreSetFailure("pin_create_failed"
                                         "schCreatePin returned nil"))
              else
                net = dbFindNetByName(cv netName)
                unless(net net = dbMakeNet(cv netName))
                term = dbFindTermByName(cv record->name)
                unless(term
                  term = dbCreateTerm(net record->name record->direction))
                unless(term && term~>net == net
                  oagitRestoreSetFailure("pin_create_failed"
                                         "pin terminal is bound to a different net"))
                when(term && !oagitRestoreErrorCode
                  pin = schCreatePin(cv master record->name record->direction nil
                                     record->xy record->orient)
                  unless(pin
                    oagitRestoreSetFailure("pin_create_failed"
                                           "schCreatePin returned nil"))))
        )
        oagitRestoreCloseMaster(master openedByScript)
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCreatePorts(cv ports pins)
  let((port pin netName net term)
    foreach(port ports
      unless(oagitRestoreErrorCode
        netName = port->name
        foreach(pin pins
          when(equal(oagitRestoreString(pin->name)
                    oagitRestoreString(port->name))
            when(pin->net && strlen(pin->net) > 0
              netName = pin->net)
          )
        )
        net = dbFindNetByName(cv netName)
        unless(net net = dbMakeNet(cv netName))
        unless(net
          oagitRestoreSetFailure("port_create_failed"
                                 "unable to create declared port net"))
        when(net && !oagitRestoreErrorCode
          term = dbFindTermByName(cv port->name)
          unless(term
            term = dbCreateTerm(net port->name port->direction))
          unless(term && term~>net == net
            oagitRestoreSetFailure("port_create_failed"
                                   "declared port is bound to a different net"))
        )
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCreateWires(cv records)
  let((record routeMethod wireWidth wire wireResult label labelResult)
    foreach(record records
      unless(oagitRestoreErrorCode
        routeMethod = record->route_method
        unless(routeMethod routeMethod = "full")
        wireWidth = if(record->width && numberp(record->width) && record->width > 0.0
                        float(record->width)
                        0.0)
        wireResult = schCreateWire(cv "draw" routeMethod record->points 0 0 wireWidth)
        wire = car(wireResult)
        unless(wire
          oagitRestoreSetFailure("wire_create_failed"
                                 "schCreateWire returned no wire"))
        when(wire && !oagitRestoreErrorCode
          when(wireWidth > 0.0
            cond(
              (equal(wire~>objType "path")
                wire~>width = wireWidth)
              (numberp(wire~>width) && abs(wire~>width - wireWidth) <= 1.0e-6
                t)
              (t
                oagitRestoreSetFailure(
                  "wire_create_failed"
                  "non-zero wire width cannot be applied to non-path wire"))
            ))
          foreach(label record->labels
            unless(oagitRestoreErrorCode
              labelResult = schCreateWireLabel(
                cv wire label->xy label->text label->justify label->orient
                label->font label->height nil)
              unless(labelResult
                oagitRestoreSetFailure("wire_label_create_failed"
                                       "schCreateWireLabel returned nil"))
            )
          )
        )
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCreateLabels(cv records)
  let((record label)
    foreach(record records
      unless(oagitRestoreErrorCode
        label = dbCreateLabel(
          cv list(record->layer record->purpose) record->xy record->text
          record->justify record->orient record->font record->height)
        unless(label
          oagitRestoreSetFailure("label_create_failed"
                                 "dbCreateLabel returned nil"))
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreCreateAnnotations(cv records)
  let((record kind layer purpose points bBox shape)
    foreach(record records
      unless(oagitRestoreErrorCode
        kind = oagitRestoreString(record->kind)
        layer = if(record->layer oagitRestoreString(record->layer) "text")
        purpose = if(record->purpose oagitRestoreString(record->purpose) "drawing")
        points = record->points
        cond(
          (equal(kind "rect")
            if(points && listp(points) && length(points) == 2
                then
                  bBox = points
                  shape = dbCreateRect(cv list(layer purpose) bBox)
                  unless(shape
                    oagitRestoreSetFailure("annotation_create_failed"
                                           "dbCreateRect returned nil"))
                else
                  oagitRestoreSetFailure("annotation_create_failed"
                                         "rect annotation requires exactly 2 bBox points")))
          (equal(kind "polygon")
            if(points && listp(points) && length(points) >= 3
                then
                  shape = dbCreatePolygon(cv list(layer purpose) points)
                  unless(shape
                    oagitRestoreSetFailure("annotation_create_failed"
                                           "dbCreatePolygon returned nil"))
                else
                  oagitRestoreSetFailure("annotation_create_failed"
                                         "polygon annotation requires at least 3 points")))
          (equal(kind "line")
            if(points && listp(points) && length(points) >= 2
                then
                  shape = dbCreateLine(cv list(layer purpose) points)
                  unless(shape
                    oagitRestoreSetFailure("annotation_create_failed"
                                           "dbCreateLine returned nil"))
                else
                  oagitRestoreSetFailure("annotation_create_failed"
                                         "line annotation requires at least 2 points")))
          (t
            oagitRestoreSetFailure(
              "annotation_create_failed"
              sprintf(nil "unsupported annotation kind: %s" kind)))
        )
      )
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRestoreFindConnection(connections name)
  let((found)
    found = nil
    foreach(pair connections
      when(equal(oagitRestoreString(car(pair)) oagitRestoreString(name))
        found = pair
      )
    )
    found
  )
)

procedure(oagitRestoreFindInstTerm(inst name)
  let((found)
    found = nil
    foreach(instTerm inst~>instTerms
      when(equal(oagitRestoreString(instTerm~>name) oagitRestoreString(name))
        found = instTerm
      )
    )
    found
  )
)

procedure(oagitRestoreActualNet(instTerm)
  if(instTerm && instTerm~>net
      then instTerm~>net~>name
      else nil)
)

procedure(oagitRestoreMismatch(instanceName terminalName expected actual reason)
  list('jsonObject
       list("instance" oagitRestoreString(instanceName))
       list("terminal" oagitRestoreString(terminalName))
       list("expected" oagitRestoreMaybeString(expected))
       list("actual" oagitRestoreMaybeString(actual))
       list("reason" reason))
)

procedure(oagitRestoreBuildExplicitNetTable(req)
  let((tbl port pin wire label text)
    tbl = makeTable("oagitExplicitNets" nil)
    when(req
      foreach(port req->ports
        when(port->name && strlen(oagitRestoreString(port->name)) > 0
          tbl[oagitRestoreString(port->name)] = t
        )
      )
      foreach(pin req->pins
        when(pin->name && strlen(oagitRestoreString(pin->name)) > 0
          tbl[oagitRestoreString(pin->name)] = t
        )
        when(pin->net && strlen(oagitRestoreString(pin->net)) > 0
          tbl[oagitRestoreString(pin->net)] = t
        )
      )
      foreach(wire req->wires
        foreach(label wire->labels
          text = oagitRestoreString(label->text)
          when(strlen(text) > 0
            tbl[text] = t
          )
        )
      )
    )
    tbl
  )
)

procedure(oagitRestoreIsAnonNetP(netName explicitNetTable)
  stringp(netName) &&
  rexMatchp("^net[0-9]+$" netName) &&
  !(explicitNetTable && explicitNetTable[netName])
)

procedure(oagitRestoreVerifyConnectivity(records instByName @optional (req nil))
  let((record inst pair expectedTerm instTerm expected actual
       explicitNetTable expToAct actToExp expStr actStr expAnon actAnon
       mappedAct mappedExp)
    oagitRestoreMismatches = nil
    explicitNetTable = oagitRestoreBuildExplicitNetTable(req)
    expToAct = makeTable("oagitAnonExpToAct" nil)
    actToExp = makeTable("oagitAnonActToExp" nil)
    foreach(record records
      inst = instByName[record->name]
      foreach(pair record->connections
        expectedTerm = car(pair)
        expected = cadr(pair)
        instTerm = oagitRestoreFindInstTerm(inst expectedTerm)
        if(instTerm
            then
              actual = oagitRestoreActualNet(instTerm)
              expStr = oagitRestoreString(expected)
              actStr = if(actual oagitRestoreString(actual) "")
              expAnon = oagitRestoreIsAnonNetP(expStr explicitNetTable)
              actAnon = actual && oagitRestoreIsAnonNetP(actStr explicitNetTable)
              cond(
                (!expAnon
                  unless(actual && equal(expStr actStr)
                    oagitRestoreMismatches = cons(
                      oagitRestoreMismatch(record->name expectedTerm expected actual
                                           "net_mismatch")
                      oagitRestoreMismatches)))
                (!actAnon
                  oagitRestoreMismatches = cons(
                    oagitRestoreMismatch(record->name expectedTerm expected actual
                                         "net_mismatch")
                    oagitRestoreMismatches))
                (t
                  mappedAct = expToAct[expStr]
                  mappedExp = actToExp[actStr]
                  cond(
                    (!mappedAct && !mappedExp
                      expToAct[expStr] = actStr
                      actToExp[actStr] = expStr)
                    (equal(mappedAct actStr) && equal(mappedExp expStr)
                      t)
                    (mappedAct && !equal(mappedAct actStr)
                      oagitRestoreMismatches = cons(
                        oagitRestoreMismatch(record->name expectedTerm expected actual
                                             "anon_net_split")
                        oagitRestoreMismatches))
                    (t
                      oagitRestoreMismatches = cons(
                        oagitRestoreMismatch(record->name expectedTerm expected actual
                                             "anon_net_short")
                        oagitRestoreMismatches))
                  ))
              )
            else
              oagitRestoreMismatches = cons(
                oagitRestoreMismatch(record->name expectedTerm expected nil
                                     "missing_terminal")
                oagitRestoreMismatches))
      )
      foreach(instTerm inst~>instTerms
        unless(oagitRestoreFindConnection(record->connections instTerm~>name)
          oagitRestoreMismatches = cons(
            oagitRestoreMismatch(record->name instTerm~>name nil
                                 oagitRestoreActualNet(instTerm)
                                 "unexpected_terminal")
            oagitRestoreMismatches)
        )
      )
    )
    oagitRestoreMismatches = reverse(oagitRestoreMismatches)
    if(oagitRestoreMismatches
        then oagitRestoreSetFailure("connectivity_mismatch"
                                    "instance terminal connectivity mismatch")
        else t)
  )
)

procedure(oagitRestoreCleanupDraft(lib draft_cell view cv)
  let((purgeResult draftDd deleteResult cleanupOk)
    cleanupOk = t
    when(cv
      purgeResult = errset(dbPurge(cv) t)
      unless(purgeResult && car(purgeResult)
        cleanupOk = nil
        oagitRestoreErrorCode = "cleanup_failed"
        oagitRestoreErrorMessage = "dbPurge failed during draft cleanup"
      )
    )
    draftDd = ddGetObj(lib draft_cell)
    when(draftDd
      deleteResult = errset(ddDeleteObj(draftDd) t)
      unless(deleteResult && car(deleteResult)
        cleanupOk = nil
        oagitRestoreErrorCode = "cleanup_failed"
        oagitRestoreErrorMessage = "ddDeleteObj failed during draft cleanup"
      )
    )
    cleanupOk
  )
)

procedure(oagitRestoreBuild(req cv instByName)
  let((checkTuple checkErrors checkWarnings)
    oagitRestoreCreateInstances(cv req->instances instByName req)
    unless(oagitRestoreErrorCode
      oagitRestoreCreatePins(cv oagitRestoreOrderRecords(req->pins req->port_order))
    )
    unless(oagitRestoreErrorCode
      oagitRestoreCreatePorts(cv oagitRestoreOrderRecords(req->ports req->port_order)
                              req->pins)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreCreateWires(cv req->wires)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreCreateLabels(cv req->labels)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreCreateAnnotations(cv req->annotations)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreApplyProps(cv req->properties nil)
    )
    unless(oagitRestoreErrorCode
      checkTuple = schCheck(cv)
      checkErrors = car(checkTuple)
      checkWarnings = cadr(checkTuple)
      oagitRestoreSchErrors = checkErrors
      oagitRestoreSchWarnings = checkWarnings
      when(checkErrors > 0
        oagitRestoreSetFailure("sch_check_failed"
                               "schCheck reported errors"))
    )
    unless(oagitRestoreErrorCode
      oagitRestoreVerifyConnectivity(req->instances instByName req)
    )
    unless(oagitRestoreErrorCode
      oagitRestoreCdfVerified = t
      oagitRestoreSaveResult = errset(dbSave(cv) t)
      unless(oagitRestoreSaveResult && car(oagitRestoreSaveResult)
        oagitRestoreSetFailure("save_failed" "dbSave failed"))
    )
    !oagitRestoreErrorCode
  )
)

procedure(oagitRunRestore(req)
  let((target lib cell view draft_cell overwrite cv
       buildResult instByName copyResult pubCv stillOpen cleanupResult)
    oagitRestoreErrorCode = nil
    oagitRestoreErrorMessage = nil
    oagitRestoreSchErrors = 0
    oagitRestoreSchWarnings = 0
    oagitRestoreCdfVerified = nil
    oagitRestoreMismatches = nil
    oagitRestoreCdfPasses = 0
    oagitRestoreCallbackOrder = nil
    oagitRestoreDerivedRefresh = nil
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft_cell = req->draft_cell
    overwrite = req->overwrite
    cond(
      (!member(lib req->allow_libraries)
        oagitRestoreFail(req "disallowed_library"
                         "target library is not allow-listed"))
      (oagitDirHasLiveLocks(lib cell view)
        oagitRestoreFail(req "live_lock"
                         "target view has a live edit lock"))
      (ddGetObj(lib cell view) && !overwrite
        oagitRestoreFail(req "target_exists"
                         "target CellView already exists"))
      (equal(draft_cell cell)
        oagitRestoreFail(req "draft_exists"
                         "draft cell must be distinct from target cell"))
      (ddGetObj(lib draft_cell view)
        oagitRestoreFail(req "draft_exists"
                         "draft CellView already exists"))
      (t
        cv = dbOpenCellViewByType(lib draft_cell view "schematic" "w")
        unless(cv
          oagitRestoreFail(req "draft_open_failed"
                           "unable to open draft CellView"))
        when(cv
          instByName = makeTable("oagitRestoreInstances" nil)
          buildResult = errset(
            oagitRestoreBuild(req cv instByName)
            t)
          unless(buildResult
            oagitRestoreSetFailure(
              "restore_failed"
              sprintf(nil "unexpected restore error: %L" errset.errset))
          )
          if(oagitRestoreErrorCode
              then
                oagitRestoreCleanupDraft(lib draft_cell view cv)
                oagitRestoreWriteResponse(
                  req nil nil oagitRestoreSchErrors oagitRestoreSchWarnings
                  oagitRestoreCdfVerified oagitRestoreMismatches)
              else
                copyResult = errset(
                  dbCopyCellView(cv lib cell view nil nil t)
                  t)
                pubCv = nil
                unless(copyResult && car(copyResult)
                  oagitRestoreSetFailure("publish_failed"
                                         "dbCopyCellView failed"))
                if(oagitRestoreErrorCode
                    then
                      oagitRestoreCleanupDraft(lib draft_cell view cv)
                      oagitRestoreWriteResponse(
                        req nil nil oagitRestoreSchErrors
                        oagitRestoreSchWarnings oagitRestoreCdfVerified
                        oagitRestoreMismatches)
                    else
                      pubCv = car(copyResult)
                      when(pubCv
                        errset(dbClose(pubCv) t)
                        errset(dbPurge(pubCv) t)
                      )
                      stillOpen = dbFindOpenCellViewByName(lib cell view)
                      when(stillOpen
                        errset(dbClose(stillOpen) t)
                        errset(dbPurge(stillOpen) t)
                      )
                      cleanupResult = oagitRestoreCleanupDraft(
                        lib draft_cell view cv)
                      unless(cleanupResult
                        unless(oagitRestoreErrorCode
                          oagitRestoreSetFailure(
                            "cleanup_failed" "draft cleanup failed")))
                      if(oagitRestoreErrorCode
                          then
                            oagitRestoreWriteResponse(
                              req nil nil oagitRestoreSchErrors
                              oagitRestoreSchWarnings oagitRestoreCdfVerified
                              oagitRestoreMismatches)
                          else
                            oagitRestoreWriteResponse(
                              req t t oagitRestoreSchErrors
                              oagitRestoreSchWarnings oagitRestoreCdfVerified
                              nil))
                  )
              )
          )
        )
    )
  )
)

unless(isCallable('oagitJsonEncode)
  let((commonPath)
    commonPath = getShellEnvVar("OAGIT_COMMON_IL")
    when(commonPath load(commonPath))
  )
)

unless(boundp('oagitRequest)
  let((requestPath)
    requestPath = getShellEnvVar("OAGIT_REQUEST_IL")
    when(requestPath load(requestPath))
  )
)

when(boundp('oagitRequest) &&
     oagitRequest->action == "restore_schematic"
  oagitRunRestore(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
