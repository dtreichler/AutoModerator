function expand(a) {
    var b = $(a.nextElementSibling);
    b.show('slow').removeClass('collapsed').addClass('expanded');
    a.onclick=function(){collapse(a)};
}
function collapse(a) {
    var b = $(a.nextElementSibling);
    b.hide('slow').removeClass('expanded').addClass('expanded');
    a.onclick=function(){expand(a)};
}
