import pytest
from glom.tutorial import Contact, ContactManager


def test_contact_manager_all_returns_contacts():
    contacts = Contact.objects.all()
    assert isinstance(contacts, list)
    assert len(contacts) >= 4
    for c in contacts:
        assert isinstance(c, Contact)
        assert hasattr(c, "id")
        assert hasattr(c, "name")


def test_contact_manager_all_after_save():
    initial_contacts = Contact.objects.all()
    initial_count = len(initial_contacts)
    new_contact = Contact("Test Resident", location="Earth")
    new_contact.save()

    updated_contacts = Contact.objects.all()
    assert len(updated_contacts) == initial_count + 1
    assert any(c.name == "Test Resident" for c in updated_contacts)
