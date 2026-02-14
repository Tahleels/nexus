// Navigation functions for submenus only
function toggleSubmenu(submenuId) {
    const submenu = document.getElementById(submenuId);
    if (submenu) {
        submenu.classList.toggle('show');

        // Toggle chevron icon
        const chevron = submenu.previousElementSibling.querySelector('.fa-chevron-down, .fa-chevron-up');
        if (chevron) {
            chevron.classList.toggle('fa-chevron-down');
            chevron.classList.toggle('fa-chevron-up');
        }
    }
}

// Initialize submenus based on current page
document.addEventListener('DOMContentLoaded', function () {
    // Auto-expand submenus if current page is in a submenu
    const currentPath = window.location.pathname;

    if (currentPath.includes('/bi-agents') || currentPath.includes('/reasoning-agents')) {
        document.getElementById('agents-submenu').classList.add('show');
        const chevron = document.querySelector('#agents-submenu').previousElementSibling.querySelector('.fa-chevron-down');
        if (chevron) {
            chevron.classList.remove('fa-chevron-down');
            chevron.classList.add('fa-chevron-up');
        }
    }

    if (currentPath.includes('/database-connections') || currentPath.includes('/knowledge-base')) {
        document.getElementById('source-submenu').classList.add('show');
        const chevron = document.querySelector('#source-submenu').previousElementSibling.querySelector('.fa-chevron-down');
        if (chevron) {
            chevron.classList.remove('fa-chevron-down');
            chevron.classList.add('fa-chevron-up');
        }
    }


    if (currentPath.includes('/jobs') || currentPath.includes('/job-monitor')) {
        const jobsSubmenu = document.getElementById('jobs-submenu');
        if (jobsSubmenu) {
            jobsSubmenu.classList.add('show');
            const chevron = jobsSubmenu.previousElementSibling.querySelector('.fa-chevron-down');
            if (chevron) {
                chevron.classList.remove('fa-chevron-down');
                chevron.classList.add('fa-chevron-up');
            }
        }
    }

});