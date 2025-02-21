import api_wrapper from './api_wrapper';
import node_details_api from './node_details';
import image_details_api from './image_details';
import * as visit_count_api from './visit_count';
import config from '../global_config';

/** Class to access the OneZoom web API, in which API requests are queued so that they do not overload the server. A single instance of the APImanager class should be present for each OneZoom application. The application should request any non-static content through that instance. 
*/
class APIManager {
  constructor() {
  }
  
  /** Set the urls that will be used when calling the API.
   *  @params {Object.<string, string>} server_urls - a set of urls for the OneZoom API. 
   * Names for each url can be one of 'search_api', 'search_sponsor_api', 'ott2id_arry_api',
   * 'otts2vns_api', 'search_init_api', 'node_details_api', 
   */
  set_urls(server_urls) {
    for (let name in server_urls) {
      if (server_urls.hasOwnProperty(name) && config.api.hasOwnProperty(name)) {
        config.api[name] = server_urls[name];
      }
    }
  }
  
  /** Starts the API queue, collecting API requests and making intermittent API calls to 
   * record the places visited on the tree
   */
  start() {
    visit_count_api.start();   
    node_details_api.start(); 
    image_details_api.start();
  }

  
  /**
   * @params {String} query
   */
  search(params) {
    params.url = config.api.search_api;
    if (params.url)
      api_wrapper(params);
    else
      alert('Something’s not quite right.' +
       '\n(search_api was not set in config.api).' +
       '\nPlease email mail@onezoom.org and let us know.')
  } 
  search_sponsor(params) {
    if (config.lang) params.data.lang = config.lang;
    params.url = config.api.search_sponsor_api;
    if (params.url)
      api_wrapper(params);
    else
      alert('Something’s not quite right.' +
       '\n(search_sponsor_api was not set in config.api).' +
       '\nPlease email mail@onezoom.org and let us know.')
  }
  ott2id_arry(params) {
    params.url = config.api.ott2id_arry_api;
    api_wrapper(params);
  }
  otts2vns(params) {
    //returns vernaculars, so we have to set the language if necessary
    if (config.lang) params.data.lang = config.lang;
    params.url = config.api.otts2vns_api;
    api_wrapper(params);
  }
  search_init(params) {
    params.url = config.api.search_init_api;
    api_wrapper(params);
  }
  node_detail(params) {
    //node_detail contains vernaculars, so we have to set the language if necessary
    if (config.lang) params.data.lang = config.lang;
    params.url = config.api.node_details_api;
    api_wrapper(params);
  }
  tour_detail(params) {
    //this returns an HTML web page fragment so we have to set the language if necessary
    if (config.lang) params.data.lang = config.lang;
    params.url = config.api.tourstop_page;
    api_wrapper(params);    
  }
}

let api_manager = new APIManager();
export default api_manager;