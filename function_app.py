import logging

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

import azure.durable_functions as df
import azure.functions as func

from functions.activity_log_bp import bp as activity_log_bp
from functions.compute_skus_bp import bp as compute_skus_bp
from functions.cr_usage_bp import bp as cr_usage_bp
from functions.data_collection_bp import bp as data_collection_bp
from functions.location_reference_bp import bp as location_reference_bp
from functions.subscription_context_bp import bp as subscription_context_bp
from functions.subscription_reference_bp import bp as subscription_reference_bp
from functions.odcr_usage_bp import bp as odcr_usage_bp
from functions.odcr_coverage_bp import bp as odcr_coverage_bp
from functions.vm_usage_bp import bp as vm_usage_bp
from functions.web_bp import bp as web_bp
from functions.zone_mapping_bp import bp as zone_mapping_bp

# DFApp extends FunctionApp, so plain @app.route triggers work alongside Durable ones.
# Each pipeline lives in its own blueprint under functions/ to keep this file thin.
# ANONYMOUS: the app is fronted by Easy Auth (App Service Authentication) in Azure;
# local func-tools runs without auth.
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)
app.register_blueprint(vm_usage_bp)
app.register_blueprint(cr_usage_bp)
app.register_blueprint(activity_log_bp)
app.register_blueprint(zone_mapping_bp)
app.register_blueprint(compute_skus_bp)
app.register_blueprint(subscription_reference_bp)
app.register_blueprint(location_reference_bp)
app.register_blueprint(subscription_context_bp)
app.register_blueprint(data_collection_bp)
app.register_blueprint(odcr_usage_bp)
app.register_blueprint(odcr_coverage_bp)
app.register_blueprint(web_bp)
