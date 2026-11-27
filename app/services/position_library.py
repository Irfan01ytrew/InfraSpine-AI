"""A starter library of common positions across job families and industries.

Positions are job titles only. Adding one from this library never grants a permission: what a person may do
in the software comes from their software role, custom roles and departments.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LibraryPosition:
    title: str
    job_family: str
    level: int | None
    description: str


def _p(title: str, family: str, level: int | None, description: str) -> LibraryPosition:
    return LibraryPosition(title, family, level, description)


POSITION_LIBRARY: tuple[LibraryPosition, ...] = (
    # Executive
    _p("Chief Executive Officer (CEO)", "executive", 1, "Leads the organization and its strategy."),
    _p("Chief Operating Officer (COO)", "executive", 1, "Runs day-to-day operations."),
    _p("Chief Financial Officer (CFO)", "executive", 1, "Leads finance, accounting and financial planning."),
    _p("Chief Technology Officer (CTO)", "executive", 1, "Leads technology and engineering."),
    _p("Chief Information Officer (CIO)", "executive", 1, "Leads information systems and IT."),
    _p("Chief Human Resources Officer (CHRO)", "executive", 1, "Leads people, culture and HR."),
    _p("Chief Marketing Officer (CMO)", "executive", 1, "Leads marketing and brand."),
    _p("Chief Information Security Officer (CISO)", "executive", 1, "Leads information security and risk."),
    _p("Managing Director", "executive", 1, "Leads a company, subsidiary or region."),
    _p("Board Member", "governance", 1, "Member of the board of directors."),
    # Management
    _p("Director", "management", 2, "Leads a function or division."),
    _p("General Manager", "management", 2, "Leads a business unit, branch or site."),
    _p("Branch Manager", "management", 3, "Leads a branch or location."),
    _p("Department Manager", "management", 3, "Leads a department."),
    _p("Project Manager", "management", 4, "Plans and delivers projects."),
    _p("Program Manager", "management", 3, "Leads a portfolio of related projects."),
    _p("Product Manager", "management", 4, "Owns a product's direction and roadmap."),
    _p("Operations Manager", "management", 4, "Runs operational processes and teams."),
    _p("Team Lead", "management", 5, "Leads a small team day to day."),
    _p("Supervisor", "management", 6, "Supervises a shift or crew."),
    # Professional
    _p("Accountant", "professional", 6, "Keeps accounts and prepares financial statements."),
    _p("Financial Analyst", "professional", 6, "Analyses financial performance and forecasts."),
    _p("Auditor", "governance", 6, "Reviews records and controls independently."),
    _p("HR Officer", "professional", 6, "Handles recruitment, records and employee relations."),
    _p("Recruiter", "professional", 6, "Finds and hires candidates."),
    _p("Legal Counsel", "professional", 4, "Advises on legal matters and contracts."),
    _p("Procurement Officer", "professional", 6, "Buys goods and services and manages suppliers."),
    _p("Business Analyst", "professional", 6, "Turns business needs into requirements."),
    _p("Data Analyst", "professional", 6, "Analyses data and builds reports."),
    _p("Marketing Specialist", "professional", 6, "Plans and runs marketing activities."),
    _p("Teacher", "professional", 6, "Teaches and assesses students."),
    _p("Doctor", "professional", 4, "Diagnoses and treats patients."),
    _p("Nurse", "professional", 6, "Provides patient care."),
    _p("Pharmacist", "professional", 5, "Dispenses medicines and advises on their use."),
    _p("Researcher", "professional", 5, "Plans and carries out research."),
    _p("Architect", "professional", 5, "Designs buildings and spaces."),
    # Technical
    _p("Software Engineer", "technical", 6, "Designs and builds software."),
    _p("Senior Software Engineer", "technical", 5, "Leads technical design and mentors engineers."),
    _p("Data Engineer", "technical", 6, "Builds data pipelines and platforms."),
    _p("DevOps Engineer", "technical", 6, "Runs build, deployment and infrastructure."),
    _p("IT Support Specialist", "technical", 7, "Supports users, devices and accounts."),
    _p("Network Engineer", "technical", 6, "Designs and runs networks."),
    _p("Security Analyst", "technical", 6, "Monitors and responds to security events."),
    _p("Civil Engineer", "technical", 5, "Designs and supervises construction works."),
    _p("Site Engineer", "technical", 6, "Supervises work on a construction site."),
    _p("Mechanical Engineer", "technical", 5, "Designs and maintains mechanical systems."),
    _p("Electrical Engineer", "technical", 5, "Designs and maintains electrical systems."),
    _p("Technician", "technical", 7, "Installs, maintains and repairs equipment."),
    _p("Quality Inspector", "governance", 7, "Checks products and work against standards."),
    # Operations and frontline
    _p("Operations Officer", "operations", 7, "Carries out operational processes."),
    _p("Logistics Coordinator", "operations", 7, "Plans shipments and deliveries."),
    _p("Warehouse Supervisor", "operations", 6, "Runs warehouse shifts and stock."),
    _p("Store Keeper", "operations", 8, "Receives, stores and issues stock."),
    _p("Driver", "frontline", 9, "Transports goods or people."),
    _p("Machine Operator", "frontline", 9, "Operates production machinery."),
    _p("Production Worker", "frontline", 9, "Works on the production line."),
    _p("Security Guard", "frontline", 9, "Protects people, premises and assets."),
    _p("Cleaner", "frontline", 10, "Keeps premises clean and safe."),
    _p("Electrician", "frontline", 8, "Installs and repairs electrical wiring."),
    _p("Plumber", "frontline", 8, "Installs and repairs plumbing."),
    _p("Cashier", "frontline", 9, "Handles customer payments."),
    _p("Customer Service Representative", "frontline", 8, "Helps customers by phone, chat or in person."),
    _p("Call Center Agent", "frontline", 9, "Answers and resolves customer calls."),
    # Sales
    _p("Sales Manager", "sales", 4, "Leads a sales team and targets."),
    _p("Sales Executive", "sales", 7, "Sells to and manages customers."),
    _p("Account Manager", "sales", 6, "Looks after key customer accounts."),
    _p("Business Development Manager", "sales", 5, "Finds new customers and partnerships."),
    # Administration
    _p("Administrative Assistant", "administration", 8, "Supports a team with scheduling and records."),
    _p("Executive Assistant", "administration", 7, "Supports a senior leader."),
    _p("Receptionist", "administration", 9, "Welcomes visitors and handles calls."),
    _p("Office Manager", "administration", 6, "Runs office facilities and services."),
    _p("Data Entry Operator", "administration", 9, "Enters and checks records."),
    # Governance
    _p("Compliance Officer", "governance", 5, "Keeps the organization within laws and policies."),
    _p("Risk Manager", "governance", 4, "Identifies and manages business risk."),
    _p("Internal Auditor", "governance", 5, "Audits internal controls and processes."),
    _p("Data Protection Officer", "governance", 4, "Oversees personal-data protection."),
    # External
    _p("Contractor", "external", None, "Works for the organization under contract."),
    _p("Consultant", "external", None, "Advises the organization from outside."),
    _p("Intern", "external", 10, "Temporary trainee."),
    _p("Vendor Representative", "external", None, "Represents a supplier."),
)
