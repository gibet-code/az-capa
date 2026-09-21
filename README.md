# Azure Capacity

Azure Capacity is an Azure Functions application for collecting Azure capacity,
usage, reference, and operational data and presenting ODCR-oriented reports.

> **Disclaimer:** Although Azure Capacity is developed by Microsoft employees,
> it is not a Microsoft service or product. Azure Capacity is an independently
> driven project. There are no implicit or explicit obligations related to this
> project; it is provided "as is," with no warranties, and confers no rights.

## Features

- **ODCR coverage report:** Assess VM eligibility, prerequisites, reservation
	status, and coverage needs through interactive charts and detailed tables.
- **ODCR usage report:** Analyze unused capacity-reservation hours and cost by
	reservation or reservation group over 1-day, 7-day, and 30-day periods.
- **Shared coverage decisions:** Record whether a VM requires ODCR coverage,
	including priority, notes, editor identity, and update time.
- **Interactive analysis and export:** Filter and group report data, switch between
	logical and physical availability zones, include deleted reservations, and
	export filtered results to CSV.
- **User-scoped Azure access:** Reuse each signed-in user's Azure token and RBAC
	permissions so reports expose only resources that user is allowed to access.
- **Asynchronous data collection:** Use Durable Functions to collect and cache
	subscriptions, locations, availability-zone mappings, Compute SKUs, VM and CR
	usage, and Activity Log data for responsive reporting.
- **Business Context fields:** Add instance-specific dimensions such as Business
	Unit, Environment, or Cost Center from subscription tags, management-group
	hierarchy, or CSV/XLSX mapping files.
- **Admin and User roles:** Give users access to reports while reserving settings,
	Business Context management, collection controls, and run history for admins.
- **Centralized collection administration:** Schedule, enable, refresh, flush,
	monitor, and review the history and progress of backend data pipelines.


## Getting started

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fgibet-code%2Faz-capa%2Fmain%2Finfra%2FmainTemplate.json/createUIDefinitionUri/https%3A%2F%2Fraw.githubusercontent.com%2Fgibet-code%2Faz-capa%2Fmain%2Finfra%2FcreateUiDefinition.json)

The deployment wizard defaults to private Function and storage connectivity.
It provisions Azure infrastructure only; Entra registration, Easy Auth, backend
role assignment, and application package deployment remain separate steps.

See [Install Azure Capacity in Azure](docs/operations/installation.md) for the
complete installation procedure.

## License

Azure Capacity is licensed under the [Apache License 2.0](LICENSE).
Third-party components remain subject to their respective licenses.