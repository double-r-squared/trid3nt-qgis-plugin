# USGS groundwater field measurements, recorded

`dm_field_measurements_2022q2.json` is the USGS Water Data OGC API's own answer, recorded verbatim, to

    GET https://api.waterdata.usgs.gov/ogcapi/v0/collections/field-measurements/items
        ?parameter_code=72019,72150,62610,62611,61055
        &datetime=2022-04-01T00:00:00Z/2022-06-30T23:59:59Z&f=json&limit=10000
        &bbox=-93.6729,41.6625,-93.6369,41.6895

(the Des Moines River water-table canary ring): 18 dated readings at six wells, read on 2022-04-25 and 2022-05-02.
